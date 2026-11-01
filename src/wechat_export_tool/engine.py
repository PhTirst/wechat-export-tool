"""Conversation-scoped export from authenticated, consistent local snapshots."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from chatlog_keeper import wechat_db as wx
from chatlog_keeper import wechat_link_fetcher as link_cache

from .backend import account_cache, load_page_keys
from .config import ExportConfig
from .errors import ExportError
from .models import Account, Conversation
from .security import private_directory, private_text_writer, secure_file
from .storage import frozen_families, readonly

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
_SHARD = re.compile(r"message_[0-9]+\.db\Z")


def discover_accounts(data_root: Path) -> list[Account]:
    root = Path(data_root).expanduser()
    if not root.is_dir():
        return []
    return [Account(path.name, path) for path in sorted(root.iterdir())
            if path.is_dir() and not path.is_symlink() and (path / "db_storage").is_dir()]


def _source(config: ExportConfig, relative: str) -> Path:
    root = config.account_root.resolve(strict=True)
    candidate = config.account_root / relative
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ExportError("database_source_outside_account")
    current = candidate
    while current != config.account_root and current != current.parent:
        stat = current.lstat()
        if current.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise ExportError("database_source_is_reparse_point")
        current = current.parent
    return candidate


@contextmanager
def _decrypted(sources: list[Path], keys: dict[bytes, bytes]):
    with frozen_families(sources) as (root, snapshots):
        result = {}
        logger = logging.getLogger(wx.__name__)
        previous = logger.disabled
        logger.disabled = True
        try:
            for index, source in enumerate(sources):
                with snapshots[source].open("rb") as handle:
                    page = handle.read(4096)
                key = keys.get(page[:16])
                if key is None:
                    raise ExportError("database_key_missing_run_capture")
                if len(page) != 4096 or not wx._verify_key_v4(key, page):
                    raise ExportError("database_page_key_invalid")
                output = root / f"plain-{index:04d}.db"
                if not wx._decrypt_db_v4_snapshot(snapshots[source], key, output):
                    raise ExportError("database_decryption_or_wal_verification_failed")
                secure_file(output)
                with readonly(output) as connection:
                    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise ExportError("database_integrity_check_failed")
                result[source] = output
            yield result
        finally:
            logger.disabled = previous


def _contacts(connection) -> list[Conversation]:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(contact)")}
    if "username" not in columns:
        raise ExportError("unsupported_contact_schema")
    fields = [name for name in ("username", "remark", "nick_name", "alias", "local_type") if name in columns]
    records = []
    for values in connection.execute("SELECT " + ",".join('"' + f + '"' for f in fields) + " FROM contact"):
        row = dict(zip(fields, values))
        identifier = str(row.get("username") or "")
        if not identifier:
            continue
        remark, nickname, alias = (str(row.get(k) or "") for k in ("remark", "nick_name", "alias"))
        group = identifier.endswith("@chatroom") or row.get("local_type") == 2
        records.append(Conversation(identifier, remark or nickname or alias or identifier,
                                    "group" if group else "contact", remark=remark,
                                    nickname=nickname, alias=alias))
    # A duplicated native ID is never selected by an ambiguous display name.
    result = {item.conversation_id: item for item in records}
    return sorted(result.values(), key=lambda item: (item.display_name.casefold(), item.conversation_id))


def list_conversations(config: ExportConfig, query: str = "", kind: str = "all") -> list[Conversation]:
    if kind not in ("all", "contact", "group"):
        raise ExportError("conversation_kind_invalid")
    source = _source(config, "db_storage/contact/contact.db")
    keys = load_page_keys(config)
    try:
        with _decrypted([source], keys) as plain, readonly(plain[source]) as connection:
            records = _contacts(connection)
    finally:
        keys.clear()
    needle = query.casefold().strip()
    return [item for item in records if (kind == "all" or item.kind == kind) and
            (not needle or any(needle in value.casefold() for value in
             (item.display_name, item.conversation_id, item.remark, item.nickname, item.alias)))]


def _json_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes_base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def _is_self(sender: str, account_id: str) -> bool:
    return bool(sender and (sender == account_id or
                (sender.startswith("wxid_") and account_id.startswith(sender + "_"))))


def _normalize_rows(raw_rows: list[dict], conversation: Conversation,
                    config: ExportConfig, contact_connection) -> tuple[list[dict], Counter]:
    messages, unparsed = [], Counter()
    display_names = {conversation.conversation_id: conversation.display_name}
    with patch.object(link_cache, "load_cached", return_value=None):
        for item in raw_rows:
            row, sender = item["row"], item["sender_username"]
            kind = int(row.get("local_type") or 0) & 0xffff
            timestamp = float(row.get("create_time") or 0)
            if not math.isfinite(timestamp):
                raise ExportError("message_timestamp_invalid")
            # WeChat may append NUL padding outside an otherwise valid XML doc.
            content = wx._decompress_message(row.get("message_content")).rstrip("\x00\r\n\t ")
            metadata = None
            if kind == 1:
                first = content.split("\n", 1)[0].strip().rstrip(":")
                if "\n" in content and first == sender:
                    content = content.split("\n", 1)[1]
                content = content.strip()
            else:
                xml = content
                content = wx._extract_wechat_xml(xml, kind).strip()
                try:
                    metadata = wx._extract_attachment_meta(xml, kind)
                except (ValueError, TypeError, OverflowError):
                    metadata = None
                if kind == 3 and isinstance(metadata, dict):
                    file_id = wx._extract_file_md5_from_packed_info(row.get("packed_info_data"))
                    if file_id:
                        metadata["file_md5"] = file_id
            parsed = bool(content)
            if not parsed:
                unparsed[str(kind)] += 1
                content = f"[未解析消息，类型 {kind}；原始内容保留在 raw_messages.jsonl]"
            self_sender = _is_self(sender, config.account_id or "")
            if self_sender:
                name = "我"
            else:
                if sender not in display_names:
                    found = contact_connection.execute(
                        "SELECT remark,nick_name,alias FROM contact WHERE username=? LIMIT 1", (sender,)).fetchone()
                    display_names[sender] = next((str(v) for v in (found or ()) if v), sender or "未知发送者")
                name = display_names[sender]
            messages.append({"ts": timestamp,
                "ts_iso": datetime.fromtimestamp(timestamp, SHANGHAI).isoformat(),
                "sender": name, "sender_wxid": sender, "chat_room": conversation.display_name,
                "conversation_id": conversation.conversation_id, "content": content,
                "msg_type": kind, "is_self": self_sender, "is_group_chat": conversation.kind == "group",
                "server_id": str(row.get("server_id") or ""), "local_id": row.get("local_id"),
                "attachment_meta": _json_value(metadata), "parse_status": "parsed" if parsed else "unparsed"})
    return messages, unparsed


def _bind_media(messages, images, voices, files):
    for message in messages:
        kind, sid = message["msg_type"], message["server_id"]
        valid = sid not in ("", "0")
        items, expected = [], None
        if kind in (3, 47):
            expected = "image"
            if valid:
                items = images.get(sid, [])
        elif kind == 34:
            expected = "audio"
            if valid and sid in voices:
                items = [voices[sid]]
        elif kind in (43, 49):
            expected = "video" if kind == 43 else ("file" if
                (message.get("attachment_meta") or {}).get("kind") == "file" else None)
            if valid:
                items = files.get(sid, [])
        if expected and not items:
            items = [{"kind": expected, "status": "missing", "path": "", "name": "附件",
                      "reason": "本机未找到能唯一匹配且通过校验的附件"}]
        message["media"] = [dict(item) for item in items]


def _save_json(path: Path, value):
    with private_text_writer(path) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def export_conversation(config: ExportConfig, conversation_id: str, output: Path) -> dict:
    from .emoji_attachments import load_emoji_metadata
    from .image_attachments import collect_images, selected_emoji_md5s
    from .image_keys import accept_candidate, load as load_image_key
    from .voice_attachments import collect_voice
    from .video_file_attachments import collect_video_files
    from .render_media_viewer import render_viewer
    from .verify_media_export import verify

    output = Path(output).expanduser().resolve()
    if output.is_relative_to(config.account_root.resolve()):
        raise ExportError("output_must_be_outside_account")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ExportError("output_directory_must_be_empty")
    contact = _source(config, "db_storage/contact/contact.db")
    message_root = config.account_root / "db_storage/message"
    databases = [_source(config, "db_storage/message/" + p.name) for p in sorted(message_root.glob("message_*.db"))
                 if _SHARD.fullmatch(p.name)]
    if not databases:
        raise ExportError("message_databases_missing")
    keys = load_page_keys(config)
    raw_rows = []
    try:
        with _decrypted([contact, *databases], keys) as plain, readonly(plain[contact]) as contacts:
            selected = [c for c in _contacts(contacts) if c.conversation_id == conversation_id]
            if len(selected) != 1:
                raise ExportError("conversation_id_not_found")
            conversation = selected[0]
            table = "Msg_" + hashlib.md5(conversation_id.encode("utf-8")).hexdigest()
            for source in databases:
                with readonly(plain[source]) as connection:
                    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    connection.row_factory = sqlite3.Row
                    cursor = connection.execute(f'SELECT rowid AS __export_rowid__,* FROM "{table}" ORDER BY create_time,sort_seq,rowid')
                    for record in cursor:
                        row = dict(record)
                        sender = wx._load_sender_names_for_ids(connection, [row.get("real_sender_id")]).get(row.get("real_sender_id"), "")
                        raw_rows.append({"source_shard": source.name, "conversation_id": conversation_id,
                                         "row": row, "sender_username": sender})
            raw_rows.sort(key=lambda item: (int(item["row"].get("create_time") or 0),
                int(item["row"].get("sort_seq") or 0), item["source_shard"], item["row"]["__export_rowid__"]))
            if not raw_rows:
                raise ExportError("conversation_has_no_local_messages")
            messages, unparsed = _normalize_rows(raw_rows, conversation, config, contacts)
        private_directory(output)
        with private_text_writer(output / "raw_messages.jsonl") as handle:
            for item in raw_rows:
                handle.write(json.dumps(_json_value(item), ensure_ascii=False) + "\n")
        cache = account_cache(config)
        image_cache = cache / "image-key.dpapi.json"
        image_key, xor_key = load_image_key(image_cache, account_root=config.account_root)
        candidate = cache / "image-candidate.dpapi.json"
        if image_key is None and candidate.is_file():
            image_key, xor_key = accept_candidate(candidate, account_root=config.account_root,
                conversation_id=conversation_id, cache_file=image_cache)
        emoji_meta = {}
        emoji_source = config.account_root / "db_storage/emoticon/emoticon.db"
        if emoji_source.is_file():
            with emoji_source.open("rb") as handle:
                ready = handle.read(16) in keys
            if ready:
                emoji_source = _source(config, "db_storage/emoticon/emoticon.db")
                with _decrypted([emoji_source], keys) as plain, readonly(plain[emoji_source]) as connection:
                    emoji_meta = load_emoji_metadata(connection, selected_emoji_md5s(messages, raw_rows))
        images = collect_images(messages, raw_rows, output, account_root=config.account_root,
            image_key=image_key, xor_key=xor_key, ffmpeg=config.ffmpeg, emoji_metadata=emoji_meta)
        voices = collect_voice(messages, output, account_root=config.account_root, page_keys=keys,
            ffmpeg=config.ffmpeg, node=config.node, silk_module=config.silk_module)
        files = collect_video_files(messages, raw_rows, output, account_root=config.account_root,
            page_keys=keys, ffmpeg=config.ffmpeg, ffprobe=config.ffprobe)
        _bind_media(messages, images, voices, files)
        counts, missing = Counter(), Counter()
        for message in messages:
            for item in message["media"]:
                counts[f"{message['msg_type']}:{item['kind']}:{item['status']}"] += 1
                if item["status"] != "available":
                    missing[item.get("reason") or "附件未通过解码或完整性验证"] += 1
                    item["reason"] = item.get("reason") or "附件未通过解码或完整性验证"
            # Page keys are never part of an export; omit per-media AES values
            # from the friendly JSON as well. The raw archive is lossless.
            if isinstance(message.get("attachment_meta"), dict):
                message["attachment_meta"] = {k: v for k, v in message["attachment_meta"].items()
                                                if k.lower() not in ("aeskey", "aes_key")}
        manifest = {"schema_version": 1, "status": "complete", "target": conversation.display_name,
            "conversation_id": conversation_id, "kind": conversation.kind,
            "generated_at": datetime.now(SHANGHAI).isoformat(), "timezone": "Asia/Shanghai",
            "messages": len(messages), "raw_rows": len(raw_rows), "unparsed_by_type": dict(unparsed),
            "media_references": sum(counts.values()), "counts": dict(counts), "missing_reasons": dict(missing),
            "note": "仅导出此电脑已保存且通过验证的附件；缺失项保留原因。完整原始记录见 raw_messages.jsonl。",
            "scope": "One selected local conversation; no CDN requests; no automatic client restart."}
        _save_json(output / "messages.json", messages)
        _save_json(output / "manifest.json", manifest)
        render_viewer(messages, output / "index.html", manifest)
        validation = verify(output, expect_count=len(messages), expect_target=conversation.display_name,
                            deep=True, ffprobe=str(config.ffprobe) if config.ffprobe else None)
        if not validation.get("verified"):
            manifest["status"] = "failed_validation"
            manifest["validation_issues"] = validation.get("issues", {})
            _save_json(output / "manifest.json", manifest)
            raise ExportError("export_validation_failed")
        return {"status": "complete", "output_directory": str(output), "messages": len(messages),
                "counts": dict(counts), "missing_reasons": dict(missing), "verified": True}
    finally:
        keys.clear()
