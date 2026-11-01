"""UI-independent jobs and services; workers never call Tk or display secrets."""
from __future__ import annotations

import json
import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import ConfigError, ExportConfig
from .errors import ExportError
from .models import Account, Conversation


def safe_error_code(error: BaseException) -> str:
    if isinstance(error, ExportError):
        return error.code
    return "configuration_invalid" if isinstance(error, ConfigError) else "operation_failed"


def display_text(value: object) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value or "")).split())


def filter_conversations(items: list[Conversation], query: str = "", kind: str = "all") -> list[Conversation]:
    if kind not in {"all", "contact", "group"}:
        raise ConfigError("会话类型无效。")
    needle = query.strip().casefold()
    return [item for item in items if (kind == "all" or item.kind == kind) and
            (not needle or any(needle in text.casefold() for text in
                (item.display_name, item.conversation_id, item.remark, item.nickname, item.alias)))]


def new_export_directory(parent: Path) -> Path:
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_%f")
    return Path(parent).expanduser().absolute() / ("wechat_" + stamp)


_STATES = {"idle", "waiting_for_wechat", "waiting_for_database", "watching_login", "watching_images",
           "scanning", "completed", "partial", "timeout", "stopped", "failed", "unsupported",
           "waiting_for_databases", "settling", "access_denied", "arming_login_capture", "account_mismatch", "stopping"}


def clean_progress(event: object) -> dict:
    if not isinstance(event, dict):
        return {}
    clean = {}
    if isinstance(event.get("state"), str) and event["state"] in _STATES:
        clean["state"] = event["state"]
    for name in ("running", "ready_for_login", "image_probe_enabled", "image_key_captured"):
        if type(event.get(name)) is bool:
            clean[name] = event[name]
    for name in ("verified_databases", "required_databases"):
        value = event.get(name)
        if type(value) is int and 0 <= value <= 0xffffffff:
            clean[name] = value
    return clean


@dataclass(frozen=True)
class JobEvent:
    operation: str
    kind: str
    value: object = None


class JobRunner:
    """A single worker; completion events are consumed on the GUI main thread."""

    def __init__(self):
        self.events: queue.Queue[JobEvent] = queue.Queue(maxsize=256)
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._operation = ""

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, operation: str, work: Callable[[threading.Event, Callable], object]) -> None:
        if self.busy:
            raise RuntimeError("job_already_running")
        self._operation = operation
        self._cancel = threading.Event()

        def progress(value):
            clean = clean_progress(value)
            if clean:
                try:
                    self.events.put_nowait(JobEvent(operation, "progress", clean))
                except queue.Full:
                    pass

        def run():
            try:
                result = work(self._cancel, progress)
            except BaseException as error:
                self.events.put(JobEvent(operation, "error", safe_error_code(error)))
            else:
                self.events.put(JobEvent(operation, "done", result))

        self._thread = threading.Thread(target=run, name="wechat-export-" + operation, daemon=True)
        self._thread.start()

    def cancel_capture(self) -> bool:
        if self.busy and self._operation == "capture":
            self._cancel.set()
            return True
        return False


class LocalServices:
    demo = False

    def accounts(self, config: ExportConfig) -> list[Account]:
        from .engine import discover_accounts
        return discover_accounts(config.data_root)

    def conversations(self, config: ExportConfig) -> list[Conversation]:
        from .engine import list_conversations
        return list_conversations(config)

    def export(self, config: ExportConfig, conversation: Conversation, destination: Path) -> dict:
        from .engine import export_conversation
        return export_conversation(config, conversation.conversation_id, destination)

    def capture(self, config: ExportConfig, *, timeout: int, conversation_id: str | None,
                cancel_event: threading.Event, progress_cb: Callable) -> dict:
        from .backend import capture_keys, prepare_image_probe
        probe = prepare_image_probe(config, conversation_id) if conversation_id else None
        return capture_keys(config, timeout=timeout, images_probe=probe,
                            cancel_event=cancel_event, progress_cb=progress_cb)


class DemoServices:
    """Entirely generated fixtures for GUI previews; no account or process access."""
    demo = True

    def accounts(self, config: ExportConfig) -> list[Account]:
        return [Account("demo-account", config.data_root / "demo-account", "合成演示账号")]

    def conversations(self, config: ExportConfig) -> list[Conversation]:
        return [Conversation("demo-contact-a", "演示好友", "contact", 5, remark="演示好友"),
                Conversation("demo-contact-b", "演示好友", "contact", 5, nickname="同名示例"),
                Conversation("demo-group@chatroom", "项目讨论群（演示）", "group", 5),
                Conversation("demo-family@chatroom", "家庭群（演示）", "group", 5)]

    def capture(self, config: ExportConfig, **kwargs) -> dict:
        return {"returncode": 0, "status": "completed", "demo": True}

    def export(self, config: ExportConfig, conversation: Conversation, destination: Path) -> dict:
        import io
        import math
        import struct
        import wave
        from collections import Counter
        from PIL import Image
        from .render_media_viewer import render_viewer
        from .security import private_directory, private_binary_writer, private_text_writer
        from .verify_media_export import verify

        output = Path(destination).resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ExportError("output_directory_must_be_empty")
        assets = private_directory(output / "attachments")
        picture = io.BytesIO()
        Image.new("RGB", (480, 240), (24, 138, 98)).save(picture, format="PNG")
        with private_binary_writer(assets / "demo.png") as handle:
            handle.write(picture.getvalue())
        audio = io.BytesIO()
        with wave.open(audio, "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
            handle.writeframes(b"".join(struct.pack("<h", int(900 * math.sin(2 * math.pi * 440 * i / 24000)))
                                      for i in range(24000)))
        with private_binary_writer(assets / "demo.wav") as handle:
            handle.write(audio.getvalue())
        with private_text_writer(assets / "demo.txt") as handle:
            handle.write("Synthetic demonstration attachment.\n")
        start = datetime(2025, 1, 1, 12, tzinfo=timezone(timedelta(hours=8)))
        definitions = [(1, "这是合成演示数据，不包含真实聊天。", []),
            (3, "[图片]", [{"kind": "image", "path": "attachments/demo.png", "name": "demo.png",
                           "mime": "image/png", "status": "available", "reason": ""}]),
            (34, "[语音 1s]", [{"kind": "audio", "path": "attachments/demo.wav", "name": "demo.wav",
                              "mime": "audio/wav", "status": "available", "reason": ""}]),
            (49, "[文件] demo.txt", [{"kind": "file", "path": "attachments/demo.txt", "name": "demo.txt",
                                    "mime": "text/plain", "status": "available", "reason": ""}]),
            (49, "[文件] 缺失示例", [{"kind": "file", "path": "", "name": "missing.txt", "mime": "",
                                    "status": "missing", "reason": "合成演示：附件未保存在本机"}])]
        messages = []
        for index, (kind, content, media) in enumerate(definitions):
            stamp = start + timedelta(minutes=index)
            messages.append({"ts": stamp.timestamp(), "ts_iso": stamp.isoformat(), "sender": "演示联系人",
                "sender_wxid": "demo-contact", "chat_room": conversation.display_name,
                "conversation_id": conversation.conversation_id, "content": content, "msg_type": kind,
                "is_self": bool(index % 2), "is_group_chat": conversation.kind == "group",
                "server_id": str(1000 + index), "local_id": index + 1,
                "attachment_meta": None, "parse_status": "parsed", "media": media})
        counts = Counter(f"{m['msg_type']}:{a['kind']}:{a['status']}" for m in messages for a in m["media"])
        missing = {"合成演示：附件未保存在本机": 1}
        summary = {"schema_version": 1, "status": "complete", "target": conversation.display_name,
                   "conversation_id": conversation.conversation_id, "messages": len(messages),
                   "raw_rows": len(messages), "media_references": sum(counts.values()),
                   "counts": dict(counts), "missing_reasons": missing,
                   "note": "这些消息、图片与声音均由程序生成，仅用于演示。"}
        for name, value in (("messages.json", messages), ("manifest.json", summary)):
            with private_text_writer(output / name) as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
        with private_text_writer(output / "raw_messages.jsonl") as handle:
            for message in messages:
                handle.write(json.dumps({"source_shard": "synthetic", "conversation_id": conversation.conversation_id,
                                         "row": message}, ensure_ascii=False) + "\n")
        render_viewer(messages, output / "index.html", summary)
        validation = verify(output, expect_count=5, expect_target=conversation.display_name, deep=True)
        if not validation.get("verified"):
            raise ExportError("export_validation_failed")
        return {"status": "complete", "output_directory": str(output), "messages": 5,
                "counts": dict(counts), "missing_reasons": missing, "verified": True, "demo": True}
