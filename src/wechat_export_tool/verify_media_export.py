"""Validate a local media export using only aggregate diagnostics.

The verifier reads only ``messages.json``, ``index.html``, ``manifest.json``
and files referenced by the manifest's messages. It never opens databases,
keys, or source-account locations.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import warnings
import wave
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


_KINDS = {"image", "audio", "video", "file"}
_STATUSES = {"available", "missing"}
_PATH_FIELDS = ("path", "original_path", "poster")
_REMOTE = re.compile(r"^(?:https?:)?//", re.I)
_REMOTE_CSS = re.compile(r"(?:url\s*\(|@import\s+)[\s\"']*(?:https?:)?//", re.I)
_TOKEN = re.compile(r"^[A-Za-z0-9_,.+-]{1,100}$")
# Reject network protocols and playlist demuxers before inspecting an export.
# An attachment's extension alone cannot distinguish a video from an HLS URL list.
_PROBE_PROTOCOLS = "file,pipe"
_PROBE_FORMATS = "mov,matroska,webm,wav,mp3,ogg,flac,aac"
_CSP_DIRECTIVES = {
    "default-src", "img-src", "media-src", "style-src", "script-src",
    "connect-src", "object-src", "base-uri", "form-action",
}
_REASON_CATEGORIES = {
    "emoji_md5_unavailable", "unrecognized_image_format",
    "image_decode_validation_failed", "image_key_or_payload_invalid",
    "image_key_unavailable", "image_xor_key_unavailable", "invalid_dat_header",
    "local_image_reference_unavailable", "ambiguous_raw_message",
    "local_file_read_failed", "local_file_not_found",
}


class _HTMLInspection(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.json_parts: list[str] = []
        self.info_parts: list[str] = []
        self.active_json: str | None = None
        self.conversation_scripts = 0
        self.info_scripts = 0
        self.executable_scripts: list[dict[str, str | None]] = []
        self.script_nonce_values: list[str] = []
        self.active_content_risks = 0
        self.csp: list[str] = []
        self.external_resources = 0
        self.text_parts: list[str] = []
        self.style_parts: list[str] = []
        self.in_style = False
        self.scripts: list[dict[str, str | None]] = []
        self.script_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag.lower() in {"iframe", "object", "embed", "base"}:
            self.active_content_risks += 1
        if any(name.lower().startswith("on") for name, _ in attrs):
            self.active_content_risks += 1
        if tag == "style":
            self.in_style = True
        if tag == "meta" and attributes.get("http-equiv", "").lower() == "content-security-policy":
            if attributes.get("content"):
                self.csp.append(attributes["content"] or "")
        if tag == "script":
            self.scripts.append(attributes)
            self.script_depth += 1
            if attributes.get("id") == "conversation-data":
                self.active_json = "conversation"
                self.conversation_scripts += 1
                if attributes.get("type") != "application/json" or attributes.get("src"):
                    self.external_resources += 1
            elif attributes.get("id") == "viewer-info":
                self.active_json = "info"
                self.info_scripts += 1
                if attributes.get("type") != "application/json" or attributes.get("src"):
                    self.external_resources += 1
            elif attributes.get("type") != "application/json":
                self.executable_scripts.append(attributes)
                nonce = attributes.get("nonce")
                if nonce:
                    self.script_nonce_values.append(nonce)
        for name, value in attrs:
            if name in {"src", "href", "poster", "action", "formaction", "data", "srcset"}:
                if value and any(_REMOTE.match(part.strip()) for part in value.split(",")):
                    self.external_resources += 1
            if name == "style" and value and _REMOTE_CSS.search(value):
                self.external_resources += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self.in_style = False
        if tag == "script":
            if self.script_depth:
                self.script_depth -= 1
            self.active_json = None

    def handle_data(self, data: str) -> None:
        if self.in_style:
            self.style_parts.append(data)
        if self.active_json == "conversation":
            self.json_parts.append(data)
        elif self.active_json == "info":
            self.info_parts.append(data)
        else:
            self.text_parts.append(data)


def _safe_file(directory: Path, raw: object) -> tuple[Path | None, str]:
    if not isinstance(raw, str) or not raw:
        return None, "path_empty"
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    if (normalized.startswith("/") or any(not part or part in {".", ".."} or ":" in part
                                          for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in normalized)):
        return None, "path_unsafe"
    candidate = directory.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(directory)
    except ValueError:
        return None, "path_escapes_directory"
    except (OSError, RuntimeError):
        return None, "path_missing"
    try:
        if not resolved.is_file() or resolved.stat().st_size == 0:
            return None, "path_not_nonempty_file"
    except OSError:
        return None, "path_unreadable"
    return resolved, ""


def _media_signature(media: dict[str, Any]) -> tuple[Any, ...]:
    return (str(media.get("kind", "file")), str(media.get("status", "missing")),
            *(str(media.get(key) or "").replace("\\", "/") for key in _PATH_FIELDS),
            bool(media.get("thumbnail")))


def _message_date(message: dict[str, Any]) -> tuple[str, str]:
    iso = str(message.get("ts_iso") or "")
    if len(iso) >= 10 and iso[4:5] == "-" and iso[7:8] == "-":
        return iso[:10], iso[:19].replace("T", " ")
    try:
        stamp = float(message.get("ts", 0))
        if stamp > 1e11:
            stamp /= 1000
        local = datetime.fromtimestamp(stamp, timezone(timedelta(hours=8)))
        return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "", "时间未知"


def _parse_csp(parser: _HTMLInspection) -> bool:
    if len(parser.csp) != 1:
        return False
    directives: dict[str, list[str]] = {}
    for segment in parser.csp[0].split(";"):
        bits = segment.strip().split()
        if bits:
            name = bits[0].lower()
            if name not in _CSP_DIRECTIVES or name in directives:
                return False
            directives[name] = bits[1:]
    if set(directives) != _CSP_DIRECTIVES:
        return False
    for name in ("default-src", "connect-src", "object-src", "base-uri", "form-action"):
        if [value.lower() for value in directives.get(name, [])] != ["'none'"]:
            return False
    # These directives are allowed only local resources and the viewer's
    # explicit inline styles / nonce-protected script.
    allowed = {
        "img-src": {"file:", "data:"},
        "media-src": {"file:"},
        "style-src": {"'unsafe-inline'"},
    }
    for name, permitted in allowed.items():
        values = directives.get(name)
        if values is None or not {value.lower() for value in values} <= permitted:
            return False
    scripts = directives.get("script-src", [])
    if (not scripts or any(value.lower() in {"*", "http:", "https:", "'unsafe-inline'", "'unsafe-eval'"}
                           or _REMOTE.match(value) for value in scripts)
            or len(parser.executable_scripts) != 1
            or len(parser.script_nonce_values) != 1
            or parser.executable_scripts[0].get("src") is not None
            or len(scripts) != 1
            or scripts[0] != "'nonce-" + parser.script_nonce_values[0] + "'"):
        return False
    return True


def _probe(path: Path, executable: str) -> tuple[dict[str, Any] | None, str]:
    try:
        proc = subprocess.run(
            [executable, "-v", "error", "-protocol_whitelist", _PROBE_PROTOCOLS,
             "-format_whitelist", _PROBE_FORMATS, "-show_entries",
             "format=format_name,duration:stream=codec_type,codec_name,duration,width,height,sample_rate",
             "-of", "json", str(path)], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        if proc.returncode:
            return None, "probe_failed"
        metadata = json.loads(proc.stdout)
        if not isinstance(metadata, dict) or not metadata.get("streams"):
            return None, "probe_streams_missing"
        return metadata, ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, "probe_failed"


def verify(directory: str | Path, expect_count: int | None = None,
           expect_target: str | None = None, deep: bool = False,
           probe_limit: int = 12, ffprobe: str | None = None) -> dict[str, Any]:
    """Validate a specific export and return sanitized aggregate diagnostics."""
    issues: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    result: dict[str, Any] = {"verified": False, "mode": "deep" if deep else "standard"}
    try:
        root = Path(directory).resolve(strict=True)
        if not root.is_dir():
            issues["export_directory_invalid"] += 1
            raise ValueError
        inputs = {}
        for name in ("messages.json", "manifest.json", "index.html"):
            path, error = _safe_file(root, name)
            if error:
                issues[error] += 1
                raise ValueError
            inputs[name] = path
        messages = json.loads(inputs["messages.json"].read_text(encoding="utf-8-sig"))
        manifest = json.loads(inputs["manifest.json"].read_text(encoding="utf-8-sig"))
        html_text = inputs["index.html"].read_text(encoding="utf-8-sig")
        if not isinstance(messages, list) or not isinstance(manifest, dict):
            issues["export_structure_invalid"] += 1
            raise ValueError
    except Exception:
        if not issues:
            issues["export_read_failed"] += 1
        result.update(totals=dict(totals), issues=dict(issues))
        return result

    totals["messages"] = len(messages)
    if expect_count is not None and len(messages) != expect_count:
        issues["expected_message_count_mismatch"] += 1
    if not messages or any(not isinstance(message, dict) for message in messages):
        issues["message_structure_invalid"] += 1
        result.update(totals=dict(totals), issues=dict(issues))
        return result

    conversations = {str(message.get("conversation_id") or "") for message in messages}
    targets = {str(message.get("chat_room") or "") for message in messages}
    if len(conversations) != 1 or "" in conversations:
        issues["conversation_scope_invalid"] += 1
    if len(targets) != 1 or "" in targets:
        issues["target_scope_invalid"] += 1
    target = expect_target if expect_target is not None else manifest.get("target")
    if target is not None and targets != {str(target)}:
        issues["target_scope_invalid"] += 1

    counts: Counter[str] = Counter()
    missing_reasons: Counter[str] = Counter()
    file_kinds: dict[Path, set[str]] = {}
    message_signatures: list[list[tuple[Any, ...]]] = []
    for message in messages:
        media = message.get("media", [])
        if not isinstance(media, list):
            issues["media_structure_invalid"] += 1
            media = []
        signatures: list[tuple[Any, ...]] = []
        for item in media:
            if not isinstance(item, dict):
                issues["media_structure_invalid"] += 1
                continue
            kind, status = item.get("kind", "file"), item.get("status", "missing")
            if not isinstance(kind, str) or kind not in _KINDS or not isinstance(status, str) or status not in _STATUSES:
                issues["media_classification_invalid"] += 1
            safe_kind = kind if isinstance(kind, str) and kind in _KINDS else "other"
            safe_status = status if isinstance(status, str) and status in _STATUSES else "other"
            totals["media_references"] += 1
            totals[f"{safe_kind}_{safe_status}_references"] += 1
            counts[f"{message.get('msg_type')}:{kind}:{status}"] += 1
            if status != "available":
                reason = item.get("reason", "未知原因")
                category = reason if isinstance(reason, str) and reason in _REASON_CATEGORIES else "other"
                missing_reasons[category] += 1
            signatures.append(_media_signature(item))
            for field in _PATH_FIELDS:
                raw = item.get(field)
                if not raw and not (field == "path" and status == "available"):
                    continue
                file_path, error = _safe_file(root, raw)
                if error:
                    issues[error] += 1
                elif file_path:
                    content_kind = ("image" if field == "poster" else
                                    kind if field == "path" and status == "available" else "file")
                    file_kinds.setdefault(file_path, set()).add(content_kind)
        message_signatures.append(signatures)

    totals["unique_referenced_files"] = len(file_kinds)
    if manifest.get("messages") != len(messages):
        issues["manifest_message_count_mismatch"] += 1
    if manifest.get("media_references") != totals["media_references"]:
        issues["manifest_media_count_mismatch"] += 1
    if manifest.get("counts") != dict(counts):
        issues["manifest_classification_counts_mismatch"] += 1
    # Compare reasons without returning source-provided text in diagnostics.
    manifest_reasons = manifest.get("missing_reasons")
    expected_reason_counts = Counter()
    for message in messages:
        media = message.get("media", [])
        for item in media if isinstance(media, list) else []:
            if isinstance(item, dict) and item.get("status", "missing") != "available":
                expected_reason_counts[str(item.get("reason", "未知原因"))] += 1
    if not isinstance(manifest_reasons, dict) or manifest_reasons != dict(expected_reason_counts):
        issues["manifest_missing_counts_mismatch"] += 1

    parser = _HTMLInspection()
    try:
        parser.feed(html_text)
        rows = json.loads("".join(parser.json_parts))
        info = json.loads("".join(parser.info_parts))
        if (parser.conversation_scripts != 1 or parser.info_scripts != 1
                or not isinstance(rows, list) or not isinstance(info, dict)):
            raise ValueError
        expected_info = {
            "title": str(manifest.get("conversation") or manifest.get("target") or "聊天记录"),
            "note": "；".join(str(manifest[key]) for key in ("note", "warning") if manifest.get(key)),
        }
        if info != expected_info:
            issues["html_viewer_info_mismatch"] += 1
        totals["html_messages"] = len(rows)
        if len(rows) != len(messages):
            issues["html_message_count_mismatch"] += 1
        available_html = missing_html = 0
        for index, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != 7 or not isinstance(row[6], list):
                issues["html_row_structure_invalid"] += 1
                continue
            if index >= len(messages):
                continue
            message = messages[index]
            day, display_time = _message_date(message)
            values = {key: "" if message.get(key) is None else str(message[key])
                      for key in ("sender", "content", "msg_type")}
            expected_plain = [day, display_time, values["sender"],
                              bool(message.get("is_self")), values["content"], values["msg_type"]]
            if row[:6] != expected_plain:
                issues["html_message_mismatch"] += 1
            totals["html_media_references"] += len(row[6])
            if any(not isinstance(item, dict) for item in row[6]):
                issues["html_media_mismatch"] += 1
                continue
            signatures = [_media_signature(item) for item in row[6]]
            if signatures != message_signatures[index]:
                issues["html_media_mismatch"] += 1
            available_html += sum(item.get("status") == "available" for item in row[6])
            missing_html += sum(item.get("status") != "available" for item in row[6])
        if totals["html_media_references"] != totals["media_references"]:
            issues["html_media_count_mismatch"] += 1
        totals["html_available_media"] = available_html
        totals["html_missing_media"] = missing_html
        if not _parse_csp(parser):
            issues["html_csp_invalid"] += 1
        if parser.external_resources:
            totals["external_resources"] = parser.external_resources
            issues["html_external_resource"] += parser.external_resources
        if parser.active_content_risks:
            totals["active_content_risks"] = parser.active_content_risks
            issues["html_active_content"] += parser.active_content_risks
        css_external = len(_REMOTE_CSS.findall("\n".join(parser.style_parts)))
        if css_external:
            totals["external_resources"] = totals.get("external_resources", 0) + css_external
            issues["html_external_resource"] += css_external
        text_content = " ".join(parser.text_parts)
        displayed = re.search(r"消息\s*([\d,]+)\s*条.*?可用附件\s*([\d,]+)\s*项.*?缺失或不可预览\s*([\d,]+)\s*项", text_content, re.S)
        if not displayed or tuple(int(part.replace(",", "")) for part in displayed.groups()) != (
                len(messages), available_html, missing_html):
            issues["html_summary_count_mismatch"] += 1
    except Exception:
        issues["html_data_invalid"] += 1

    try:
        from PIL import Image, ImageSequence
    except ImportError:
        Image = ImageSequence = None
    image_files = sorted((path for path, kinds in file_kinds.items() if "image" in kinds), key=str)
    if image_files and Image is None:
        issues["image_verifier_missing"] += 1
    elif Image is not None:
        for path in image_files:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(path) as image:
                        if image.width <= 0 or image.height <= 0:
                            raise ValueError
                        image.verify()
                    if deep:
                        with Image.open(path) as image:
                            for frame in ImageSequence.Iterator(image):
                                frame.load()
                totals["images_verified"] += 1
            except Exception:
                issues["image_invalid"] += 1

    probe_jobs: list[tuple[Path, str]] = []
    for path, kinds in sorted(file_kinds.items(), key=lambda item: str(item[0])):
        if "audio" in kinds and path.suffix.lower() == ".wav":
            try:
                with wave.open(str(path), "rb") as audio:
                    frames, channels = audio.getnframes(), audio.getnchannels()
                    width, rate = audio.getsampwidth(), audio.getframerate()
                    if frames <= 0 or not 1 <= channels <= 8 or width not in (1, 2, 3, 4) or rate <= 0:
                        raise ValueError
                    if deep:
                        remaining = frames
                        while remaining:
                            requested = min(remaining, 65536)
                            chunk = audio.readframes(requested)
                            if len(chunk) != requested * width * channels:
                                raise ValueError
                            remaining -= requested
                totals["wav_verified"] += 1
            except Exception:
                issues["wav_invalid"] += 1
        elif "audio" in kinds or "video" in kinds:
            probe_jobs.append((path, "video" if "video" in kinds else "audio"))
    totals["probe_candidates"] = len(probe_jobs)
    executable = ffprobe or shutil.which("ffprobe")
    selected = probe_jobs if deep or probe_limit < 0 else probe_jobs[:probe_limit]
    codecs: Counter[str] = Counter()
    containers: Counter[str] = Counter()
    if selected and not executable:
        issues["media_probe_missing"] += 1
    elif executable:
        for path, kind in selected:
            metadata, error = _probe(path, executable)
            if error:
                issues[error] += 1
                continue
            streams = metadata.get("streams", [])
            if not any(stream.get("codec_type") == kind for stream in streams if isinstance(stream, dict)):
                issues["media_expected_stream_missing"] += 1
                continue
            totals["media_probed"] += 1
            for stream in streams:
                token = str(stream.get("codec_name", "unknown"))
                codecs[token if _TOKEN.fullmatch(token) else "other"] += 1
            container = str(metadata.get("format", {}).get("format_name", "unknown"))
            containers[container if _TOKEN.fullmatch(container) else "other"] += 1

    result.update(verified=not issues, totals=dict(totals), issues=dict(issues),
                  codecs=dict(codecs), containers=dict(containers))
    return result
