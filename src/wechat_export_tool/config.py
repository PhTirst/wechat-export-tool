"""Portable user configuration, separate from local protected key storage."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path


class ConfigError(ValueError):
    """A configuration error whose message is safe to show in the CLI."""


def default_config_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return (Path(root) if root else Path.home() / "AppData" / "Local") / "WeChatExportTool"


def default_config_path() -> Path:
    return default_config_dir() / "config.json"


_PATH_FIELDS = frozenset(("data_root", "cache_dir", "capture_exe", "ffmpeg", "ffprobe", "node", "silk_module"))
_REQUIRED_PATHS = frozenset(("data_root", "cache_dir"))


@dataclass(frozen=True, slots=True)
class ExportConfig:
    data_root: Path = field(default_factory=lambda: Path.home() / "xwechat_files")
    account_id: str | None = None
    cache_dir: Path = field(default_factory=lambda: default_config_dir() / "cache")
    capture_exe: Path | None = None
    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    node: Path | None = None
    silk_module: Path | None = None

    def __post_init__(self) -> None:
        for name in _PATH_FIELDS:
            value = getattr(self, name)
            if value is None and name not in _REQUIRED_PATHS:
                continue
            if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
                raise ConfigError("配置中的路径必须为非空字符串。")
            object.__setattr__(self, name, Path(value).expanduser().absolute())
        value = self.account_id
        if value is not None and (not isinstance(value, str) or not value or value in (".", "..")
                                  or any(char in value for char in '/\\\0:<>"|?*')
                                  or any(ord(char) < 32 for char in value) or value.endswith(".")
                                  or value != value.strip()):
            raise ConfigError("账号 ID 必须是本机账号目录名称，不能包含路径。")

    @property
    def account_root(self) -> Path:
        if self.account_id is None:
            raise ConfigError("尚未选择微信账号，请使用 --account 或交互选择。")
        return self.data_root / self.account_id

    def to_dict(self) -> dict:
        values = {item.name: getattr(self, item.name) for item in fields(self)}
        return {"schema_version": 1,
                **{name: str(value) if isinstance(value, Path) else value
                   for name, value in values.items()}}

    @classmethod
    def from_dict(cls, values: dict, *, base_dir: Path | None = None) -> "ExportConfig":
        if not isinstance(values, dict):
            raise ConfigError("配置必须是 JSON 对象。")
        names = {item.name for item in fields(cls)}
        if set(values) - names - {"schema_version"}:
            raise ConfigError("配置含不支持的字段；请勿在配置中保存密钥。")
        version = values.get("schema_version", 1)
        if type(version) is not int or version != 1:
            raise ConfigError("不支持此配置版本。")
        kwargs = {name: value for name, value in values.items() if name in names}
        for name in _PATH_FIELDS & kwargs.keys():
            value = kwargs[name]
            if value is None and name not in _REQUIRED_PATHS:
                continue
            if not isinstance(value, str) or not value:
                raise ConfigError("配置中的路径必须为非空字符串。")
            path = Path(value).expanduser()
            if not path.is_absolute() and base_dir is not None:
                path = base_dir / path
            kwargs[name] = path
        return cls(**kwargs)


def load_config(path: Path | None = None) -> ExportConfig:
    path = Path(path) if path is not None else default_config_path()
    try:
        with path.open("rb") as handle:
            data = handle.read(65537)
    except FileNotFoundError:
        return ExportConfig()
    except OSError:
        raise ConfigError("无法读取配置文件，请检查目录权限。") from None
    if len(data) > 65536:
        raise ConfigError("配置文件超出大小限制。")
    try:
        values = json.loads(data)
    except (ValueError, UnicodeError):
        raise ConfigError("配置文件不是有效 JSON。") from None
    return ExportConfig.from_dict(values, base_dir=path.absolute().parent)


def save_config(config: ExportConfig, path: Path | None = None) -> Path:
    path = Path(path) if path is not None else default_config_path()
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, filename = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
        temporary = Path(filename)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(config.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        raise ConfigError("无法写入配置文件，请检查目录权限。") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path
