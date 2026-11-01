"""Read-only voice extraction from explicitly supplied local account storage.

Importing this module performs no source, key-cache, or process access. The
caller supplies account_root and authenticated page-key material; no automatic
key discovery, login, network access, or account selection occurs here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import wave
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path

from chatlog_keeper import wechat_db as wx

from .security import private_binary_writer, private_directory, private_text_writer, secure_file
from .storage import frozen_families, readonly


_SERVER_ID = re.compile(r"^-?\d{1,20}$")
_SILK_MAGIC = b"#!SILK_V3"
_MAX_VOICE_BYTES = 16 << 20
_SCRIPT = Path(__file__).parent / "assets" / "decode_voice_batch.cjs"
_REASONS = {
    "media_database_missing": "本机未找到语音数据库",
    "voice_key_missing": "本机语音数据库密钥尚未就绪",
    "voice_key_invalid": "语音数据库密钥校验失败",
    "voice_database_invalid": "语音数据库完整性校验失败",
    "voice_database_failed": "本机语音数据库读取失败",
    "session_not_in_media_index": "本机会话语音索引中没有此会话",
    "audio_missing_from_media_db": "本机未保存此条语音音频",
    "voice_identity_mismatch": "本机语音记录的时间或消息编号不一致",
    "voice_time_missing": "此条语音缺少有效的消息时间",
    "voice_blob_invalid": "本机语音音频为空或格式不受支持",
    "voice_decoder_missing": "本机语音解码器尚未就绪",
    "voice_decoder_failed": "本机语音解码未完成",
    "silk_decode_failed": "本机语音音频解码失败",
    "voice_duration_mismatch": "语音解码时长与消息记录不一致",
    "no_server_id": "此条语音缺少服务器消息编号",
    "scope_invalid": "语音导出会话范围无效",
}


def _missing(reason: str) -> dict:
    return {"kind": "audio", "path": "", "name": "语音", "mime": "audio/wav",
            "status": "missing", "reason": _REASONS.get(reason, _REASONS["voice_decoder_failed"])}


def _voice_messages(messages: list[dict]) -> list[dict]:
    result = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        try:
            kind = int(message.get("msg_type", message.get("local_type", 0))) & 0xffffffff
        except (TypeError, ValueError, OverflowError):
            continue
        if kind == 34:
            result.append(message)
    return result


def _timestamp(message: dict) -> int | None:
    try:
        value = float(message.get("ts", message.get("create_time")))
        if not math.isfinite(value) or value < 0:
            return None
        if value > 1e11:
            value /= 1000
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _local_id(message: dict) -> int | None:
    value = message.get("local_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _expected_ms(message: dict) -> int:
    metadata = message.get("attachment_meta")
    if not isinstance(metadata, dict):
        return 0
    try:
        return max(0, min(300000, int(metadata.get("voice_length_ms", 0))))
    except (TypeError, ValueError, OverflowError):
        return 0


def _voice_blob(connection: sqlite3.Connection, conversation: str, message: dict
                ) -> tuple[bytes | None, str]:
    """Match exact conversation, server ID and time, plus local ID if known."""
    server = str(message.get("server_id") or "")
    if not _SERVER_ID.fullmatch(server) or not -(2**63) <= int(server) < 2**63 or int(server) == 0:
        return None, "no_server_id"
    timestamp = _timestamp(message)
    if timestamp is None:
        return None, "voice_time_missing"
    local_id = _local_id(message)
    ids = connection.execute("SELECT rowid FROM Name2Id WHERE user_name = ?",
                             (conversation,)).fetchall()
    if not ids:
        return None, "session_not_in_media_index"
    mismatch = invalid = False
    blobs = []
    for (chat_id,) in ids:
        rows = connection.execute(
            "SELECT create_time, local_id, voice_data FROM VoiceInfo "
            "WHERE chat_name_id = ? AND svr_id = ? ORDER BY create_time DESC",
            (chat_id, int(server)),
        )
        for stored_time, stored_id, data in rows:
            if int(stored_time) != timestamp or (local_id is not None and int(stored_id) != local_id):
                mismatch = True
                continue
            if not isinstance(data, (bytes, bytearray, memoryview)):
                invalid = True
                continue
            blob = bytes(data)
            if (not 10 < len(blob) <= _MAX_VOICE_BYTES or
                    not (blob.startswith(_SILK_MAGIC) or blob.startswith(b"\x02" + _SILK_MAGIC))):
                invalid = True
                continue
            if blob not in blobs:
                blobs.append(blob)
    if len(blobs) == 1:
        return blobs[0], ""
    if len(blobs) > 1:
        return None, "voice_identity_mismatch"
    return None, ("voice_identity_mismatch" if mismatch else
                  "voice_blob_invalid" if invalid else "audio_missing_from_media_db")


def _verify_wave(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as reader:
            if (reader.getnchannels() != 1 or reader.getsampwidth() != 2 or
                    reader.getframerate() != 24000 or reader.getnframes() <= 0):
                return False
            remaining = reader.getnframes()
            while remaining:
                count = min(remaining, 65536)
                if len(reader.readframes(count)) != count * 2:
                    return False
                remaining -= count
            return True
    except (OSError, EOFError, wave.Error):
        return False


def _run_batch(jobs: list[dict], private_root: Path, *, ffmpeg: Path | None = None,
               node: Path | None = None, silk_module: Path | None = None) -> dict:
    """One Node process for the complete batch, with explicitly selected tools."""
    if not jobs:
        return {}
    executable = str(Path(node).resolve()) if node is not None else shutil.which("node")
    missing = {job["id"]: {"status": "missing", "reason": "voice_decoder_missing"} for job in jobs}
    if not executable or not Path(executable).is_file() or not _SCRIPT.is_file():
        return missing
    if silk_module is not None and not Path(silk_module).is_file():
        return missing
    manifest = {"jobs": jobs, "silkModule": str(Path(silk_module).resolve()) if silk_module else "",
                "ffmpeg": str(Path(ffmpeg).resolve()) if ffmpeg and Path(ffmpeg).is_file() else ""}
    manifest_path, result_path = private_root / "voice-jobs.json", private_root / "voice-result.json"
    with private_text_writer(manifest_path) as handle:
        json.dump(manifest, handle)
    environment = dict(os.environ)
    environment.pop("NODE_OPTIONS", None)
    environment.pop("NODE_PATH", None)
    try:
        process = subprocess.run([executable, str(_SCRIPT), str(manifest_path), str(result_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=max(120, min(1800, len(jobs) * 8)), env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode == 0 and result_path.is_file():
            secure_file(result_path)
            if result_path.stat().st_size <= max(1 << 20, len(jobs) * 256):
                decoded = json.loads(result_path.read_text(encoding="utf-8"))
                if isinstance(decoded, dict):
                    return decoded
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {job["id"]: {"status": "missing", "reason": "voice_decoder_failed"} for job in jobs}


def collect_voice(messages: list[dict], output_dir: Path, *, account_root: Path,
                  page_keys: Mapping[bytes, bytes], ffmpeg: Path | None = None,
                  node: Path | None = None, silk_module: Path | None = None,
                  progress_cb=None) -> dict[str, dict]:
    """Export the selected conversation's local voices as WAV (optionally MP3).

    Page keys are supplied by the caller and are never loaded from or written to
    a cache. Every media shard is frozen together, authenticated, decrypted with
    WAL into private temporary storage and checked. Output records contain only
    relative attachment paths. No original account database is modified.
    """
    voices = _voice_messages(messages)
    if not voices:
        return {}
    records = {str(message.get("server_id") or ""): _missing("audio_missing_from_media_db")
               for message in voices}
    conversations = {str(message.get("conversation_id") or "") for message in voices}
    if len(conversations) != 1 or not next(iter(conversations)):
        return {server: _missing("scope_invalid") for server in records}
    conversation = next(iter(conversations))
    # Conflicting duplicated server IDs must never select an arbitrary payload.
    selected = {}
    ambiguous = set()
    for message in voices:
        server = str(message.get("server_id") or "")
        signature = (_timestamp(message), _local_id(message), _expected_ms(message))
        if server in selected and selected[server][0] != signature:
            ambiguous.add(server)
        else:
            selected.setdefault(server, (signature, message))
    for server in ambiguous:
        records[server] = _missing("voice_identity_mismatch")
        selected.pop(server, None)
    if not selected:
        return records

    def progress(stage, completed=0):
        if progress_cb:
            progress_cb({"stage": stage, "completed": completed, "total": len(records),
                         "available": sum(item["status"] == "available" for item in records.values())})

    output_dir = Path(output_dir).resolve()
    directory = output_dir / "附件" / "语音"
    for folder in (output_dir, directory.parent, directory):
        private_directory(folder)
    account_root = Path(account_root).resolve()
    media_root = account_root / "db_storage" / "message"
    sources = sorted(media_root.glob("media_*.db"))
    if not sources:
        return {server: _missing("media_database_missing") for server in records}
    # Prevent candidate aliases/junctions from escaping the explicitly supplied
    # account root. Snapshot helpers independently fence file-family changes.
    try:
        for source in sources:
            source.resolve(strict=True).relative_to(account_root)
            if not re.fullmatch(r"media_\d+\.db", source.name):
                raise ValueError()
    except (OSError, ValueError, RuntimeError):
        return {server: _missing("voice_database_failed") for server in records}
    if not page_keys:
        return {server: _missing("voice_key_missing") for server in records}
    logger = logging.getLogger(wx.__name__)
    previous_disabled = logger.disabled
    logger.disabled = True
    progress("voice_databases_started")
    try:
        with frozen_families(sources) as (private_root, snapshots):
            private_root = Path(private_root)
            failures = []
            jobs = []
            with ExitStack() as stack:
                connections = []
                for index, source in enumerate(sources):
                    with snapshots[source].open("rb") as handle:
                        page = handle.read(4096)
                    key = page_keys.get(page[:16])
                    if key is None:
                        failures.append("voice_key_missing")
                        continue
                    if len(page) != 4096 or not isinstance(key, bytes) or not wx._verify_key_v4(key, page):
                        failures.append("voice_key_invalid")
                        continue
                    plain = private_root / f"voice-plain-{index:03d}.db"
                    if not wx._decrypt_db_v4_snapshot(snapshots[source], key, plain):
                        failures.append("voice_database_failed")
                        continue
                    secure_file(plain)
                    connection = stack.enter_context(readonly(plain))
                    check = connection.execute("PRAGMA quick_check").fetchone()
                    if not check or check[0] != "ok":
                        failures.append("voice_database_invalid")
                        continue
                    connections.append(connection)
                for processed, (server, (_, message)) in enumerate(selected.items(), 1):
                    reason, found_blobs = "session_not_in_media_index", []
                    for connection in connections:
                        try:
                            blob, candidate_reason = _voice_blob(connection, conversation, message)
                        except (sqlite3.Error, OverflowError, ValueError, TypeError):
                            blob, candidate_reason = None, "voice_database_failed"
                        if blob is not None and blob not in found_blobs:
                            found_blobs.append(blob)
                        if reason == "session_not_in_media_index" or candidate_reason not in (
                                "session_not_in_media_index", "audio_missing_from_media_db", ""):
                            reason = candidate_reason
                    if len(found_blobs) != 1:
                        records[server] = _missing("voice_identity_mismatch" if len(found_blobs) > 1 else
                                                   failures[0] if failures else reason)
                        continue
                    token = hashlib.sha256(server.encode("utf-8")).hexdigest()[:32]
                    raw_path = private_root / (token + ".silk")
                    with private_binary_writer(raw_path) as handle:
                        handle.write(found_blobs[0])
                    jobs.append({"id": token, "server": server, "input": str(raw_path),
                        "output": str(private_root / (token + ".wav")),
                        "mp3": str(private_root / (token + ".mp3")),
                        "expectedMs": _expected_ms(message)})
                    if processed % 100 == 0:
                        progress("voice_collected", processed)
            progress("voice_decode_started", len(selected))
            decoded = _run_batch(jobs, private_root, ffmpeg=ffmpeg, node=node, silk_module=silk_module)
            for job in jobs:
                result = decoded.get(job["id"], {})
                wav_path = Path(job["output"])
                if not isinstance(result, dict) or result.get("status") != "available" or not _verify_wave(wav_path):
                    reason = result.get("reason", "silk_decode_failed") if isinstance(result, dict) else "silk_decode_failed"
                    records[job["server"]] = _missing(reason)
                    continue
                secure_file(wav_path)
                # Node and optional ffmpeg write only below the protected
                # scratch directory. Atomic publication never follows a
                # pre-existing destination symlink or exposes partial audio.
                published_wave = directory / wav_path.name
                with wav_path.open("rb") as source, private_binary_writer(published_wave) as target:
                    shutil.copyfileobj(source, target, 1 << 20)
                secure_file(published_wave)
                selected_path, mime = published_wave, "audio/wav"
                mp3_path = Path(job["mp3"])
                if result.get("mp3") and mp3_path.is_file() and mp3_path.stat().st_size > 0:
                    secure_file(mp3_path)
                    published_mp3 = directory / mp3_path.name
                    with mp3_path.open("rb") as source, private_binary_writer(published_mp3) as target:
                        shutil.copyfileobj(source, target, 1 << 20)
                    secure_file(published_mp3)
                    selected_path, mime = published_mp3, "audio/mpeg"
                records[job["server"]] = {"kind": "audio", "status": "available",
                    "path": selected_path.relative_to(output_dir).as_posix(), "name": selected_path.name,
                    "mime": mime, "reason": ""}
    except Exception:
        for server, record in records.items():
            if record["status"] != "available":
                records[server] = _missing("voice_database_failed")
    finally:
        logger.disabled = previous_disabled
    progress("voice_complete", len(records))
    return records
