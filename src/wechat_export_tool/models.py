"""Public metadata models; these objects never contain message bodies or keys."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class Account:
    account_id: str
    path: Path
    display_name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: str
    display_name: str
    kind: Literal["contact", "group"]
    message_count: int | None = None
    remark: str = ""
    nickname: str = ""
    alias: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("contact", "group"):
            raise ValueError("conversation kind must be contact or group")
        if not isinstance(self.conversation_id, str) or not self.conversation_id:
            raise ValueError("conversation ID is required")
