"""Scoped local image/sticker export; no process, key-cache, or network access.

Only collect_images performs source I/O. All key material must be passed by the
caller. Chat images are resolved only inside the selected conversation/month.
Sticker candidates use exact message MD5s in known account-local paths.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

from .security import secure_directory, private_binary_writer
from chatlog_keeper.wechat_image import _decrypt_v1v2

HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
SHANGHAI = timezone(timedelta(hours=8))
MAX_ASSET_BYTES = 200 * 1024 * 1024
V1 = b"\x07\x08V1\x08\x07"
V2 = b"\x07\x08V2\x08\x07"
_WRITE_LOCK = threading.Lock()


def _blob(value) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, dict) and isinstance(value.get("$bytes_base64"), str):
        try:
            return base64.b64decode(value["$bytes_base64"], validate=True)
        except (ValueError, TypeError):
            return b""
    return value.encode("utf-8") if isinstance(value, str) else b""


def _pb_fields(data: bytes):
    """Bounded protobuf wire reader; no speculative search for hex substrings."""
    if len(data) > 1024 * 1024:
        return
    offset = 0

    def varint():
        nonlocal offset
        result = shift = 0
        while offset < len(data) and shift < 70:
            value = data[offset]
            offset += 1
            result |= (value & 127) << shift
            if value < 128:
                return result
            shift += 7
        raise ValueError("bad_protobuf")

    try:
        while offset < len(data):
            tag = varint()
            field, wire = tag >> 3, tag & 7
            if field == 0:
                return
            if wire == 0:
                yield field, varint()
            elif wire == 2:
                length = varint()
                end = offset + length
                if end > len(data):
                    return
                yield field, data[offset:end]
                offset = end
            elif wire in (1, 5):
                offset += 8 if wire == 1 else 4
                if offset > len(data):
                    return
            else:
                return
    except ValueError:
        return


def packed_image_id(packed) -> str:
    for field, payload in _pb_fields(_blob(packed)):
        if field == 3 and isinstance(payload, bytes):
            for inner, value in _pb_fields(payload):
                if inner == 4 and isinstance(value, bytes):
                    text = value.decode("ascii", "ignore")
                    if HEX32.fullmatch(text):
                        return text.lower()
    return ""


def _xml(row: dict):
    for column in ("message_content", "compress_content"):
        raw = _blob(row.get(column))
        if raw.startswith(b"\x28\xb5\x2f\xfd"):
            try:
                import zstandard
                raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=8 * 1024 * 1024)
            except Exception:
                continue
        if not raw or len(raw) > 8 * 1024 * 1024:
            continue
        text = raw.decode("utf-8", "replace")
        start = text.find("<")
        if start < 0 or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            continue
        try:
            return ElementTree.fromstring(text[start:].rstrip('\x00\r\n\t '))
        except ElementTree.ParseError:
            continue
    return None


def _attr(root, tag: str, name: str) -> str:
    if root is None:
        return ""
    node = root if root.tag == tag else root.find(".//" + tag)
    return str(node.get(name) or "") if node is not None else ""


def _image_format(data: bytes) -> tuple[str, str] | None:
    """Require the decoder to validate and load actual pixels."""
    try:
        from PIL import Image, ImageSequence
        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").upper()
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            for frame in ImageSequence.Iterator(image):
                frame.load()
        return {
            "JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
            "GIF": ("gif", "image/gif"), "WEBP": ("webp", "image/webp"),
            "BMP": ("bmp", "image/bmp"), "TIFF": ("tif", "image/tiff"),
        }.get(fmt)
    except Exception:
        return None


def _legacy_decode(raw: bytes) -> bytes | None:
    for magic in (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF8", b"RIFF", b"BM", b"wxgf"):
        if len(raw) < len(magic):
            continue
        key = raw[0] ^ magic[0]
        if bytes(value ^ key for value in raw[:len(magic)]) == magic:
            return bytes(value ^ key for value in raw)
    return None


def _xml_key(root, tag="img") -> bytes | None:
    text = _attr(root, tag, "aeskey")
    if HEX32.fullmatch(text):
        return bytes.fromhex(text)
    return None


def _decode_dat(raw: bytes, image_key: bytes | None, xor_key: int | None,
                xml_key: bytes | None = None) -> tuple[bytes | None, str]:
    if raw[:6] not in (V1, V2):
        direct = raw if _image_format(raw) else _legacy_decode(raw)
        return direct, "" if direct else "unrecognized_image_format"
    if raw[:6] == V1:
        keys = [b"cfcd208495d565ef"]
    else:
        keys = list(dict.fromkeys(key for key in (image_key, xml_key) if key and len(key) == 16))
        if not keys:
            return None, "image_key_unavailable"
    import struct
    try:
        aes_size, xor_size = struct.unpack_from("<II", raw, 6)
    except struct.error:
        return None, "invalid_dat_header"
    if aes_size > MAX_ASSET_BYTES or xor_size > len(raw) - 15:
        return None, "invalid_dat_header"
    guesses = [xor_key] if xor_key is not None else ([0x88] if raw[:6] == V1 else [])
    if xor_size == 0:
        guesses.append(0)
    # Recover an exact file's possible JPEG/PNG tail key. Every candidate must
    # still pass complete image decoding; there is no global hardcoded V2 key.
    if xor_key is None and xor_size >= 2:
        for tail in (b"\xff\xd9", b"IEND\xaeB`\x82"):
            if xor_size >= len(tail):
                proposed = raw[-1] ^ tail[-1]
                if bytes(value ^ proposed for value in raw[-len(tail):]) == tail:
                    guesses.append(proposed)
    if not guesses:
        return None, "image_xor_key_unavailable"
    wxgf_candidate = None
    for key in keys:
        for xor_value in dict.fromkeys(guesses):
            decoded = _decrypt_v1v2(raw, key, int(xor_value))
            if not decoded:
                continue
            if _image_format(decoded):
                return decoded, ""
            if decoded.startswith(b"wxgf"):
                wxgf_candidate = decoded
    if wxgf_candidate is not None:
        return wxgf_candidate, ""
    return None, "image_key_or_payload_invalid"


def _wxgf_preview(data: bytes, ffmpeg: Path | None = None) -> bytes | None:
    """Decode local HEVC in a child process, retaining original WXGF separately."""
    executable = str(ffmpeg) if ffmpeg is not None else shutil.which("ffmpeg")
    if not executable or not Path(executable).is_file():
        return None
    starts = [i for i in range(4, min(8192, len(data) - 4))
              if data[i:i + 4] == b"\x00\x00\x00\x01" or data[i:i + 3] == b"\x00\x00\x01"]
    offsets = list(dict.fromkeys(([starts[0]] if starts else []) + [4]))
    for offset in offsets:
        try:
            result = subprocess.run([
                executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-threads", "1", "-f", "hevc", "-i", "pipe:0", "-threads", "1",
                "-frames:v", "8", "-q:v", "2",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ], input=data[offset:], capture_output=True, timeout=35,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode or len(result.stdout) > MAX_ASSET_BYTES:
            continue
        stream, cursor, frames = result.stdout, 0, []
        while len(frames) < 8:
            start = stream.find(b"\xff\xd8", cursor)
            end = stream.find(b"\xff\xd9", start + 2) if start >= 0 else -1
            if end < 0:
                break
            frame = stream[start:end + 2]
            if _image_format(frame):
                frames.append(frame)
            cursor = end + 2
        if frames:
            from PIL import Image, ImageStat
            def score(frame):
                with Image.open(io.BytesIO(frame)) as image:
                    image = image.convert("L").resize((64, 64))
                    histogram = image.histogram()
                    return (sum(histogram[:245]), ImageStat.Stat(image).stddev[0])
            return max(frames, key=score)
    return None


def _safe_source(path: Path, account_root: Path) -> bool:
    try:
        # Reparse/junction components are rejected as well as final symlinks.
        root = account_root.resolve(strict=True)
        path.resolve(strict=True).relative_to(root)
        current = path
        while current != account_root and current != current.parent:
            stat = current.lstat()
            if current.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                return False
            current = current.parent
        return path.is_file() and 0 < path.stat().st_size <= MAX_ASSET_BYTES
    except (OSError, ValueError):
        return False


def _write(data: bytes, directory: Path, extension: str) -> Path:
    path = directory / (hashlib.sha256(data).hexdigest()[:32] + "." + extension)
    # Different local references can contain identical image bytes. Serialize
    # publication only, keeping decode/transcode parallel and ACL setup stable.
    with _WRITE_LOCK:
        if not path.exists():
            with private_binary_writer(path, secure_parent=True) as handle:
                handle.write(data)
    return path


def _materialize(data: bytes, directory: Path, output_dir: Path,
                 thumbnail: bool = False, *, ffmpeg: Path | None = None) -> dict:
    reason = "仅找到缩略图" if thumbnail else ""
    original = None
    if data.startswith(b"wxgf"):
        original = _write(data, directory, "wxgf")
        data = _wxgf_preview(data, ffmpeg)
        if not data:
            return {"kind": "image", "path": "", "name": "图片", "mime": "",
                    "status": "missing", "reason": "WXGF 原编码已保留，静态预览转换失败",
                    "original_path": original.relative_to(output_dir).as_posix()}
        reason = (reason + "；" if reason else "") + "WXGF 静态预览，原编码另存"
    detected = _image_format(data)
    if not detected:
        return {"kind": "image", "path": "", "name": "图片", "mime": "",
                "status": "missing", "reason": "image_decode_validation_failed"}
    extension, mime = detected
    path = _write(data, directory, extension)
    result = {"kind": "image", "path": path.relative_to(output_dir).as_posix(),
              "name": ("缩略图" if thumbnail else "图片") + "." + extension,
              "mime": mime, "status": "available", "reason": reason,
              "thumbnail": thumbnail}
    if original:
        result["original_path"] = original.relative_to(output_dir).as_posix()
    return result


def _missing(reason: str, name="图片") -> dict:
    return {"kind": "image", "path": "", "name": name, "mime": "",
            "status": "missing", "reason": reason}


def _month(message: dict, row: dict) -> str:
    iso = str(message.get("ts_iso") or "")
    if re.match(r"^\d{4}-\d{2}-", iso):
        return iso[:7]
    try:
        stamp = float(message.get("ts") or row.get("create_time") or 0)
        if stamp > 1e11:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, SHANGHAI).strftime("%Y-%m")
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def selected_emoji_md5s(messages: list[dict], raw_rows: list[dict]) -> set[str]:
    """Return exact XML emoji MD5s only for selected message server IDs."""
    selected = {str(message.get("server_id")) for message in messages
                if (int(message.get("msg_type") or 0) & 0xffffffff) == 47
                and message.get("server_id")}
    matches = {}
    for archived in raw_rows:
        row = archived.get("row", archived)
        sid = str(row.get("server_id") or "")
        if sid in selected and (int(row.get("local_type") or 0) & 0xffffffff) == 47:
            matches.setdefault(sid, []).append(row)
    result = set()
    for rows in matches.values():
        if len({(_blob(row.get("packed_info_data")), _blob(row.get("message_content"))) for row in rows}) != 1:
            continue
        digest = _attr(_xml(rows[0]), "emoji", "md5").lower()
        if HEX32.fullmatch(digest):
            result.add(digest)
    return result


def collect_images(messages: list[dict], raw_rows: list[dict], output_dir: Path,
                   *, account_root: Path, image_key: bytes | None = None,
                   xor_key: int | None = None, ffmpeg: Path | None = None,
                   emoji_metadata: dict | None = None, workers: int = 6,
                   progress_cb=None) -> dict[str, list[dict]]:
    """Return server-ID -> media entries, exclusively from the passed conversation.

    Emoji layout assumptions (no recursive arbitrary-account filename scan):
      business/emoticon/Persist/<md5[:2]>/<md5>
      cache/<YYYY-MM>/Emoticon/<md5[:2]>/<md5>
      business/emoticon/Thumb/<md5[:2]>/<md5>.thumb (thumbnail fallback)
    ``emoji_metadata`` may be supplied by emoji_attachments.load_emoji_metadata
    from a caller-verified, read-only database snapshot. Only exact selected
    MD5 records and explicitly linked aliases/packages are used.
    """
    conversations = {str(item.get("conversation_id")) for item in messages if item.get("conversation_id")}
    if len(conversations) != 1:
        raise ValueError("exactly_one_conversation_required")
    conversation = next(iter(conversations))
    if image_key is not None and len(image_key) != 16:
        raise ValueError("image_key_must_be_16_bytes")
    if xor_key is not None and (not isinstance(xor_key, int) or not 0 <= xor_key <= 255):
        raise ValueError("xor_key_must_be_one_byte")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 16:
        raise ValueError("workers_must_be_between_1_and_16")
    output_dir = Path(output_dir)
    account = Path(account_root)
    image_dir = output_dir / "附件" / "图片"
    emoji_dir = output_dir / "附件" / "表情"
    for directory in (output_dir, output_dir / "附件", image_dir, emoji_dir):
        secure_directory(directory)
    rows = {}
    for raw in raw_rows:
        row = raw.get("row", raw)
        server_id = str(row.get("server_id") or "")
        if server_id and server_id != "0":
            rows.setdefault(server_id, []).append(row)
    chat_root = account / "msg" / "attach" / hashlib.md5(conversation.encode("utf-8")).hexdigest()
    # Enumerate month directory names only. Candidate file reads remain exact-MD5.
    cache = account / "cache"
    cache_months = sorted((path for path in cache.iterdir() if path.is_dir() and
                           re.fullmatch(r"\d{4}-\d{2}", path.name)), reverse=True) if cache.is_dir() else []
    results, jobs, referenced_by = {}, {}, {}
    target_count = sum((int(item.get("msg_type") or 0) & 0xffffffff) in (3, 47) and
                       str(item.get("server_id") or "") not in ("", "0") for item in messages)
    completed = available = missing = 0
    def store(server_id, media):
        nonlocal completed, available, missing
        results[server_id] = [media]
        completed += 1
        available += media.get("status") == "available"
        missing += media.get("status") != "available"
        if progress_cb and (completed % 100 == 0 or completed == target_count):
            progress_cb({"completed": completed, "total": target_count,
                         "available": available, "missing": missing})
    if progress_cb:
        progress_cb({"completed": 0, "total": target_count, "available": 0, "missing": 0})
    for message in messages:
        kind = int(message.get("msg_type") or 0) & 0xffffffff
        if kind not in (3, 47):
            continue
        server_id = str(message.get("server_id") or "")
        if not server_id or server_id == "0":
            continue
        raw_matches = rows.get(server_id, [])
        matching = [row for row in raw_matches if (int(row.get("local_type") or 0) & 0xffffffff) == kind]
        if len(matching) > 1:
            # Server IDs identify records; duplicated exports must be identical
            # in their attachment lookup fields, otherwise fail closed.
            signatures = {(_blob(row.get("packed_info_data")), _blob(row.get("message_content"))) for row in matching}
            if len(signatures) != 1:
                store(server_id, _missing("ambiguous_raw_message"))
                continue
        row = matching[0] if matching else {}
        xml = _xml(row)
        candidates = []
        if kind == 3:
            meta = message.get("attachment_meta") or {}
            local_id = str(meta.get("file_md5") or "").lower()
            if not HEX32.fullmatch(local_id):
                local_id = packed_image_id(row.get("packed_info_data"))
            month = _month(message, row)
            if not local_id or not month:
                store(server_id, _missing("local_image_reference_unavailable"))
                continue
            folder = chat_root / month / "Img"
            candidates = [(folder / (local_id + suffix), thumb)
                          for suffix, thumb in (("_h.dat", False), ("_W.dat", False), (".dat", False), ("_t.dat", True))]
            xml_aes = _xml_key(xml)
            directory = image_dir
            reference = (kind, local_id, month, xml_aes)
        else:
            digest = _attr(xml, "emoji", "md5").lower()
            if not HEX32.fullmatch(digest):
                store(server_id, _missing("emoji_md5_unavailable", "表情"))
                continue
            persist = account / "business" / "emoticon"
            emeta = (emoji_metadata or {}).get(digest) or {}
            aliases = [digest] + [value.lower() for value in emeta.get("extern_md5s", [])
                                  if isinstance(value, str) and HEX32.fullmatch(value)]
            aliases = list(dict.fromkeys(aliases))
            candidates = [(persist / "Persist" / value[:2] / value, False) for value in aliases]
            month = _month(message, row)
            ordered_months = sorted(cache_months, key=lambda path: path.name != month)
            candidates.extend((path / "Emoticon" / value[:2] / value, False)
                              for value in aliases for path in ordered_months)
            packages = []
            for package in emeta.get("packages", []):
                relative = str(package.get("path") or "").replace("\\", "/")
                if not re.fullmatch(r"business/emoticon/PersistStore/[0-9a-f]{2}/[0-9a-f]{32}", relative):
                    continue
                try:
                    offset, size = int(package["offset"]), int(package["size"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if offset < 0 or not 0 < size <= MAX_ASSET_BYTES or offset > 2**63 - size:
                    continue
                packages.append((account / relative, bool(package.get("thumbnail")), offset, size))
            candidates.extend(value for value in packages if not value[1])
            candidates.extend((persist / "Thumb" / value[:2] / (value + ".thumb"), True) for value in aliases)
            candidates.extend(value for value in packages if value[1])
            xml_aes = _xml_key(xml, "emoji")
            directory = emoji_dir
            reference = (kind, digest, xml_aes, _attr(xml, "emoji", "aeskey"))
        referenced_by.setdefault(reference, []).append(server_id)
        if reference not in jobs:
            extra_keys = []
            expected_md5s = ()
            if kind == 47:
                from .emoji_attachments import aes_keys
                extra_keys = aes_keys(_attr(xml, "emoji", "aeskey"))
                extra_keys.extend(emeta.get("aes_keys", []))
                # Only the message digest and DB's explicit extern_md5 aliases;
                # candidate basenames/package names are never integrity inputs.
                expected_md5s = tuple(aliases)
            jobs[reference] = (candidates, xml_aes, directory, kind, extra_keys, expected_md5s)

    def process_reference(job):
        candidates, xml_aes, directory, kind, extra_keys, expected_md5s = job
        last_reason, saw_file, chosen = "local_file_not_found", False, None
        last_materialized = None
        for candidate_spec in candidates:
            candidate, thumbnail = candidate_spec[:2]
            if not _safe_source(candidate, account):
                continue
            saw_file = True
            try:
                if len(candidate_spec) == 4:
                    offset, size = candidate_spec[2:]
                    if offset + size > candidate.stat().st_size:
                        last_reason = "emoji_package_bounds_invalid"
                        continue
                    with candidate.open("rb") as handle:
                        handle.seek(offset)
                        raw = handle.read(size)
                    if len(raw) != size:
                        last_reason = "emoji_package_read_incomplete"
                        continue
                else:
                    raw = candidate.read_bytes()
            except OSError:
                last_reason = "local_file_read_failed"
                continue
            decoded, reason = _decode_dat(raw, image_key, xor_key, xml_aes)
            if kind == 47 and (not decoded or (not decoded.startswith(b"wxgf") and not _image_format(decoded))):
                from .emoji_attachments import decode_emoji_aes
                decoded = decode_emoji_aes(raw, extra_keys, _image_format,
                                           expected_md5s=expected_md5s)
                if decoded:
                    reason = ""
            if not decoded:
                last_reason = reason
                continue
            media = _materialize(decoded, directory, output_dir, thumbnail, ffmpeg=ffmpeg)
            if kind == 47:
                media["name"] = media["name"].replace("图片", "表情")
            if media["status"] == "available":
                chosen = dict(media)
                break
            last_reason = media["reason"]
            last_materialized = media
        if chosen is None:
            chosen = last_materialized or _missing(
                last_reason if saw_file else "本机未找到已下载的表情" if kind == 47 else "本机未找到图片文件",
                "表情" if kind == 47 else "图片")
        return chosen

    def publish(reference, media):
        # Only the calling thread changes counters, results, or invokes callback.
        for server_id in referenced_by[reference]:
            store(server_id, dict(media))

    if workers == 1:
        for reference, job in jobs.items():
            publish(reference, process_reference(job))
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="local-image") as pool:
            futures = {pool.submit(process_reference, job): reference for reference, job in jobs.items()}
            for future in as_completed(futures):
                publish(futures[future], future.result())
    return results
