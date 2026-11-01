"""Copy only explicitly selected conversation video/file attachments, offline.

All source roots, database page keys, and media executables are caller supplied.
No account data is touched at import time. Sources are restricted to the message
month and native identifiers; no account-wide filename search is performed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

_TZ = timezone(timedelta(hours=8))
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_BASE = re.compile(r"^[0-9A-Za-z_-]{16,80}$")

from .security import private_directory, secure_file
from .storage import frozen_families, readonly


def _tool(path: Path | None, command: str) -> Path | None:
    selected = path if path is not None else shutil.which(command)
    if selected is None:
        return None
    selected = Path(selected)
    return selected if selected.is_file() else None


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _bytes(value):
    if isinstance(value, dict) and "$bytes_base64" in value:
        try:
            value = base64.b64decode(value["$bytes_base64"], validate=True)
        except (ValueError, TypeError):
            return b""
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value) if isinstance(value, (bytes, bytearray, memoryview)) else b""


def _pb(blob):
    """Bounded protobuf wire reader; values are retained only for wire type 2."""
    if not blob or len(blob) > 4 * 1024 * 1024:
        return {}
    result = defaultdict(list)
    pos = 0

    def varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(blob):
                raise ValueError("truncated")
            byte = blob[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("varint")

    try:
        while pos < len(blob):
            tag = varint()
            number, wire = tag >> 3, tag & 7
            if not number:
                return {}
            if wire == 0:
                varint()
            elif wire == 1:
                pos += 8
            elif wire == 2:
                size = varint()
                end = pos + size
                if end > len(blob):
                    return {}
                result[number].append(blob[pos:end])
                pos = end
            elif wire == 5:
                pos += 4
            else:
                return {}
            if pos > len(blob):
                return {}
    except ValueError:
        return {}
    return dict(result)


def _video_base(row):
    for nested in _pb(_bytes(row.get("packed_info_data", row.get("packed_info")))).get(4, []):
        for value in _pb(nested).get(8, []):
            try:
                text = value.decode("ascii")
            except UnicodeDecodeError:
                continue
            if _BASE.fullmatch(text):
                return text
    return None


def _xml(row):
    data = _bytes(row.get("message_content"))
    if not data or len(data) > 8 * 1024 * 1024:
        return None
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            import zstandard
            data = zstandard.ZstdDecompressor().decompress(data, max_output_size=8 * 1024 * 1024)
        except Exception:
            return None
    # Do not permit entity expansion or a document type from message content.
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        return None
    start = data.find(b"<")
    try:
        return ET.fromstring(data[start:].rstrip(b'\x00\r\n\t ')) if start >= 0 else None
    except ET.ParseError:
        return None


def _file_metadata(message, row):
    meta = dict(message.get("attachment_meta") or {})
    root = _xml(row)
    app = root if root is not None and root.tag == "appmsg" else (root.find(".//appmsg") if root is not None else None)
    if app is not None:
        if _int(app.findtext("type")) != 6:
            return None
        attach = app.find("appattach")
        meta.setdefault("filename", app.findtext("title"))
        if attach is not None:
            meta.setdefault("total_bytes", _int(attach.findtext("totallen")))
            meta.setdefault("md5", attach.findtext("md5"))
            meta.setdefault("fileext", attach.findtext("fileext"))
        meta["kind"] = "file"
    return meta if meta.get("kind") == "file" else None


def _month(message, row):
    iso = str(message.get("ts_iso") or "")
    if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])-\d{2}.*", iso):
        return iso[:7]
    stamp = _int(row.get("create_time")) or _int(message.get("ts"))
    if stamp > 10**11:
        stamp //= 1000
    try:
        return datetime.fromtimestamp(stamp, _TZ).strftime("%Y-%m") if stamp else None
    except (OSError, OverflowError, ValueError):
        return None


def _source(root, relative):
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        return None
    path = root / relative
    try:
        current = path
        while current != root and current != current.parent:
            attributes = current.lstat()
            if current.is_symlink() or getattr(attributes, "st_file_attributes", 0) & 0x400:
                return None
            current = current.parent
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        return resolved if resolved.is_file() else None
    except (OSError, ValueError):
        return None


def _filename(value):
    """A basename is mandatory; never reinterpret an arbitrary private path."""
    if not isinstance(value, str):
        return None
    value = value.strip().strip("\x00")
    if not value or value in (".", "..") or any(c in value for c in '/\\\x00\r\n:'):
        return None
    return value if all(ord(c) >= 32 for c in value) else None


def _safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    stem, suffix = os.path.splitext(value)
    stem = stem[:110].strip(" .") or "附件"
    if stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        stem = "_" + stem
    return stem + suffix[:18]


def _digest(path, algorithm="md5"):
    hash_ = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hash_.update(block)
    return hash_.hexdigest()


def _copy(source, directory, name, secure_file):
    """Copy with a source consistency fence and content-addressed safe name."""
    private_directory(directory)
    before = source.stat()
    digest = _digest(source, "sha256")
    destination = directory / (digest[:16] + "_" + _safe_name(name))
    if destination.is_symlink():
        raise OSError("attachment_destination_not_regular")
    if not destination.exists():
        partial = destination.with_name(destination.name + ".part")
        try:
            with source.open("rb") as input_, partial.open("xb") as output:
                secure_file(partial)
                shutil.copyfileobj(input_, output, length=1024 * 1024)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise OSError("attachment_changed")
            if _digest(partial, "sha256") != digest:
                raise OSError("attachment_copy_changed")
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
    elif _digest(destination, "sha256") != digest:
        raise OSError("attachment_destination_conflict")
    secure_file(destination)
    return destination


def _probe(path, ffprobe):
    if ffprobe is None:
        return None
    try:
        result = subprocess.run([str(ffprobe), "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov,matroska,webm", "-show_streams", "-show_format", "-of", "json", str(path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _compatible(probe):
    if not isinstance(probe, dict):
        return False
    videos = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"]
    audio = [stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"]
    formats = set(str(probe.get("format", {}).get("format_name") or "").split(","))
    # Chromium supports these within MP4/WebM; yuv420p avoids unsupported H.264
    # chroma/high-bit-depth profiles even if the codec name itself is H.264.
    return bool(formats & {"mov", "mp4", "matroska", "webm"}) and bool(videos) and all(stream.get("codec_name") in {"h264", "vp9", "av1"} and
        (stream.get("codec_name") != "h264" or stream.get("pix_fmt") in (None, "yuv420p", "yuvj420p")) for stream in videos) and all(
        stream.get("codec_name") in {"aac", "mp3", "opus", "vorbis"} for stream in audio)


@lru_cache(maxsize=16)
def _encoder_options_cached(executable: str, _mtime_ns: int, _size: int) -> tuple[str, ...] | None:
    """Probe only the selected local binary, once per executable revision."""
    try:
        result = subprocess.run([executable, "-nostdin", "-hide_banner", "-encoders"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = result.stdout
        if result.returncode != 0 or not isinstance(raw, (str, bytes)) or len(raw) > 1 << 20:
            return None
        listing = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        encoders = set(re.findall(r"(?m)^\s*V[A-Z.]{5}\s+(libx264|h264_mf)\s", listing))
        if "libx264" in encoders:
            return ("-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p")
        if "h264_mf" in encoders:
            # FFmpeg's Media Foundation encoder accepts YUV420P and defaults
            # to Windows' software MFT. Its AVOptions do not include x264's
            # preset/crf controls. No external GPL encoder is required here.
            return ("-c:v", "h264_mf", "-hw_encoding", "0", "-b:v", "2500k", "-pix_fmt", "yuv420p")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _video_encoder_options(ffmpeg: Path) -> tuple[str, ...] | None:
    try:
        selected = Path(ffmpeg).resolve(strict=True)
        metadata = selected.stat()
        return _encoder_options_cached(str(selected), metadata.st_mtime_ns, metadata.st_size)
    except OSError:
        return None


def _playable(original, ffmpeg, ffprobe):
    if _compatible(_probe(original, ffprobe)):
        return original, None
    if ffmpeg is None or ffprobe is None:
        return None, "视频原件已保存；本机缺少兼容格式转换工具"
    converted = original.with_name(original.stem + "_播放.mp4")
    if converted.is_symlink():
        return None, "视频原件已保存；播放副本路径不是普通文件"
    if converted.is_file() and _compatible(_probe(converted, ffprobe)):
        secure_file(converted)
        return converted, None
    video_options = _video_encoder_options(ffmpeg)
    if video_options is None:
        return None, "视频原件已保存；转换工具没有可用的网页兼容 H.264 编码器"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".video-", suffix=".mp4", dir=original.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    secure_file(temporary)
    try:
        result = subprocess.run([str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm", "-i", str(original),
            "-map", "0:v:0", "-map", "0:a:0?", *video_options,
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temporary)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0 and _compatible(_probe(temporary, ffprobe)):
            secure_file(temporary)
            temporary.replace(converted)
            secure_file(converted)
            return converted, None
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return None, "视频原件已保存；转换为网页可播放格式失败"


@contextmanager
def _resources(account_root, page_keys):
    """Freeze and authenticate a private resource snapshot; never cache keys."""
    from chatlog_keeper import wechat_db as wx
    source = _source(account_root, Path("db_storage/message/message_resource.db"))
    if source is None:
        yield None
        return
    with frozen_families([source]) as (private_root, snapshots):
        frozen = snapshots[source]
        with frozen.open("rb") as handle:
            page = handle.read(4096)
        key = page_keys.get(page[:16])
        if len(page) != 4096 or key is None or not wx._verify_key_v4(key, page):
            yield None
            return
        plain = private_root / "resource-plain.db"
        if not wx._decrypt_db_v4_snapshot(frozen, key, plain):
            yield None
            return
        secure_file(plain)
        with readonly(plain) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                yield None
                return
            connection.row_factory = sqlite3.Row
            yield connection


def _raw_scope_matches(archived, row, target):
    """Validate explicit scope markers, preserving old already-scoped archives."""
    expected_table = "Msg_" + hashlib.md5(target.encode("utf-8")).hexdigest()
    for container in (archived, row):
        if container.get("conversation_id") not in (None, "", target):
            return False
        for field in ("source_table", "conversation_table"):
            if container.get(field) not in (None, "", expected_table):
                return False
    return True


def _resource_details(connection, target, server_id, row):
    if connection is None:
        return []
    try:
        columns = {item[1] for item in connection.execute("PRAGMA table_info(MessageResourceDetail)")}
        selected = [name for name in ("packed_info", "size", "type", "md5") if name in columns]
        if not selected:
            return []
        query = ("SELECT " + ",".join('d."' + name + '"' for name in selected) +
            " FROM MessageResourceDetail AS d JOIN MessageResourceInfo AS i ON d.message_id=i.message_id "
            "JOIN ChatName2Id AS c ON c.rowid=i.chat_id WHERE c.user_name=? AND i.message_svr_id=?")
        parameters = [target, server_id]
        for column, value in (("message_local_id", row.get("local_id")), ("message_create_time", row.get("create_time")),
                              ("message_local_type", row.get("local_type"))):
            if value is not None:
                query += " AND i." + column + "=?"
                parameters.append(_int(value))
        return [dict(record) for record in connection.execute(query, parameters)]
    except sqlite3.Error:
        return []


def _resource_filenames(details):
    """Current resource packed_info is a raw UTF-8 basename; accept wire text too."""
    results = []
    for detail in details:
        blob = _bytes(detail.get("packed_info"))
        texts = [blob]
        for values in _pb(blob).values():
            texts.extend(values)
        for text in texts:
            try:
                name = _filename(text.decode("utf-8"))
            except UnicodeDecodeError:
                continue
            if name:
                results.append((name, _int(detail.get("size")), detail.get("md5")))
    return results


def _entry(kind, path, output_dir, name=None):
    return {"kind": kind, "status": "available", "path": path.relative_to(output_dir).as_posix(),
            "name": name or path.name, "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}


def _missing(kind, reason):
    return {"kind": kind, "status": "missing", "path": "", "name": "视频" if kind == "video" else "文件",
            "mime": "", "reason": reason}


def collect_video_files(
    messages: list[dict], raw_rows: list[dict], output_dir: Path, *,
    account_root: Path, page_keys: Mapping[bytes, bytes],
    ffmpeg: Path | None = None, ffprobe: Path | None = None, progress_callback=None,
) -> dict[str, list[dict]]:
    """Collect only video and appmsg subtype 6 files from one conversation.

    Keys are server-id strings; values contain ``video``/``file`` media items.
    Original video and converted playback copies are represented separately.
    A missing item never falls back to an unrelated same-name file.
    ``progress_callback``, when supplied, receives a count-only dict; it never
    receives filenames, identities, message bodies, or key material.
    ``raw_rows`` must already be selected from this conversation's Msg_<MD5>
    table. New archives should carry ``conversation_id`` in the outer dict;
    explicit scope markers are checked and mismatching rows are rejected.
    """
    account_root = Path(account_root).resolve()
    ffmpeg = _tool(ffmpeg, "ffmpeg")
    ffprobe = _tool(ffprobe, "ffprobe")
    output_dir = Path(output_dir).resolve()
    targets = {str(item.get("conversation_id")) for item in messages if item.get("conversation_id")}
    if len(targets) != 1:
        raise ValueError("attachment_conversation_scope_invalid")
    target = next(iter(targets))
    by_id = defaultdict(list)
    for archived in raw_rows:
        if not isinstance(archived, dict):
            continue
        row = archived.get("row", archived)
        if isinstance(row, dict) and row.get("server_id") and _raw_scope_matches(archived, row, target):
            by_id[str(row["server_id"])].append(row)
    selected = []
    for message in messages:
        kind = _int(message.get("msg_type")) & 65535
        sid = str(message.get("server_id") or "")
        if kind not in (43, 49) or not sid:
            continue
        if message.get("conversation_id") != target:
            selected.append((message, {}, sid, kind, None))
            continue
        candidates = [row for row in by_id.get(sid, []) if (_int(row.get("local_type")) & 65535) == kind]
        if len(candidates) > 1:
            candidates = [row for row in candidates if _int(row.get("create_time")) == _int(message.get("ts"))]
        if len(candidates) != 1:
            if kind == 43 or (message.get("attachment_meta") or {}).get("kind") == "file":
                selected.append((message, {}, sid, kind, None))
            continue
        row = candidates[0]
        metadata = _file_metadata(message, row) if kind == 49 else None
        if kind == 49 and metadata is None:
            continue
        selected.append((message, row, sid, kind, metadata))
    result = {}
    if not selected:
        return result
    private_directory(output_dir)
    private_directory(output_dir / "附件")
    resource_context = _resources(account_root, page_keys) if any(item[3] == 49 for item in selected) else nullcontext(None)
    with resource_context as resources:
        for index, (message, row, sid, kind, metadata) in enumerate(selected):
            if progress_callback:
                progress_callback({"stage": "video_file_progress", "processed": index, "total": len(selected)})
            media_kind = "video" if kind == 43 else "file"
            month = _month(message, row)
            if not row or not month:
                result[sid] = [_missing(media_kind, "原始消息无法唯一匹配；未猜测附件来源")]
                continue
            try:
                if kind == 43:
                    base = _video_base(row)
                    fallback_md5 = None
                    if base is None:
                        # XML md5 is often a content checksum rather than the
                        # native packed-info id; use it only if the file hashes
                        # to that checksum, never merely by its filename.
                        candidate = str((message.get("attachment_meta") or {}).get("md5") or "").lower()
                        if _HEX32.fullmatch(candidate):
                            base = fallback_md5 = candidate
                    if base is None:
                        result[sid] = [_missing("video", "原始消息没有有效本机视频标识")]
                        continue
                    roots = [Path("msg/video") / month]
                    chat_hash = hashlib.md5(target.encode("utf-8")).hexdigest()
                    roots.extend(Path("msg/attach") / chat_hash / month / name for name in ("Video", "video"))
                    sources = [source for relative in roots for name in (base + ".mp4", base + "_raw.mp4")
                               if (source := _source(account_root, relative / name)) and source.stat().st_size > 0]
                    if fallback_md5:
                        sources = [source for source in sources if _digest(source) == fallback_md5]
                    if not sources:
                        result[sid] = [_missing("video", "本机对应月份没有已下载的视频原文件")]
                        continue
                    directory = output_dir / "附件/视频"
                    original = _copy(sources[0], directory, sources[0].name, secure_file)
                    play, reason = _playable(original, ffmpeg, ffprobe)
                    items = [_entry("video", play, output_dir)] if play else [_missing("video", reason)]
                    if play != original:
                        items.append(_entry("file", original, output_dir, "原视频" + original.suffix))
                    for source in sources[1:]:
                        if _digest(source, "sha256") != _digest(original, "sha256"):
                            copied = _copy(source, directory, source.name, secure_file)
                            items.append(_entry("file", copied, output_dir, "原视频（另一版本）" + copied.suffix))
                    for relative in roots:
                        poster = _source(account_root, relative / (base + "_thumb.jpg"))
                        if poster:
                            with poster.open("rb") as handle:
                                magic = handle.read(8)
                            if magic.startswith(b"\xff\xd8\xff") or magic.startswith(b"\x89PNG"):
                                copied = _copy(poster, directory, poster.name, secure_file)
                                items[0]["poster"] = copied.relative_to(output_dir).as_posix()
                                break
                    result[sid] = items
                else:
                    metadata = metadata or {}
                    details = _resource_details(resources, target, message["server_id"], row)
                    names = _resource_filenames(details)
                    known_name = _filename(metadata.get("filename"))
                    size = _int(metadata.get("total_bytes"))
                    md5 = str(metadata.get("md5") or "").lower()
                    if known_name:
                        names.insert(0, (known_name, size, md5))
                    matches = {}
                    for name, detail_size, detail_md5 in names:
                        source = _source(account_root, Path("msg/file") / month / name)
                        if source is None:
                            continue
                        expected_size = size or detail_size
                        expected_md5 = md5 or str(detail_md5 or "").lower()
                        if expected_size and source.stat().st_size != expected_size:
                            continue
                        if _HEX32.fullmatch(expected_md5) and _digest(source) != expected_md5:
                            continue
                        # Exact resource-server binding may supply a name even
                        # when an older XML omitted a byte count/checksum.
                        if not expected_size and not _HEX32.fullmatch(expected_md5) and not details:
                            continue
                        matches[str(source)] = (source, name)
                    if len(matches) != 1:
                        result[sid] = [_missing("file", "本机对应月份未找到唯一且通过大小/校验验证的文件")]
                        continue
                    source, name = next(iter(matches.values()))
                    copied = _copy(source, output_dir / "附件/文件", name, secure_file)
                    result[sid] = [_entry("file", copied, output_dir, name)]
            except (OSError, ValueError, sqlite3.Error):
                result[sid] = [_missing(media_kind, "附件读取或转换未通过校验；未使用其他文件代替")]
    if progress_callback:
        progress_callback({"stage": "video_file_progress", "processed": len(selected), "total": len(selected)})
    return result
