"""Discover portable runtimes without opening accounts or launching processes."""
from __future__ import annotations

import dataclasses
import importlib.util
import platform
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import ExportConfig


_LAYOUT = {
    "capture_exe": ("capture/wxmori-media.exe",),
    "node": ("node/node.exe",),
    "silk_module": ("silk/lib/index.cjs", "silk/index.cjs"),
    "ffmpeg": ("ffmpeg/ffmpeg.exe",),
    "ffprobe": ("ffmpeg/ffprobe.exe",),
}
_TOOL_NAMES = {"capture_exe": "capture", "node": "node", "silk_module": "silk",
               "ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}


def _file(path: Path | None) -> bool:
    try:
        return path is not None and path.is_file()
    except OSError:
        return False


def bundled_runtime_dir() -> Path | None:
    """The portable bundle carries tools beside the executables, never in CWD."""
    if not getattr(sys, "frozen", False):
        return None
    external = Path(sys.executable).resolve().parent / "runtime"
    if external.is_dir():
        return external
    internal = getattr(sys, "_MEIPASS", None)
    if internal and (Path(internal) / "runtime").is_dir():
        return Path(internal) / "runtime"
    return external


def _defaults() -> dict[str, Path]:
    root = bundled_runtime_dir()
    if root is not None:
        return {name: next((root / relative for relative in relatives if _file(root / relative)),
                           root / relatives[0]) for name, relatives in _LAYOUT.items()}
    project = Path(__file__).resolve().parents[2]
    candidates = {
        "capture_exe": project / "bin/wxmori-media.exe",
        "silk_module": project / "node_modules/silk-wasm/lib/index.cjs",
    }
    return {name: path for name, path in candidates.items() if _file(path)}


def apply_bundled_defaults(config: ExportConfig) -> ExportConfig:
    """Keep every explicit user override, even if it currently points nowhere."""
    frozen = bool(getattr(sys, "frozen", False))
    updates = {name: path for name, path in _defaults().items()
               if getattr(config, name) is None and (frozen or _file(path))}
    return dataclasses.replace(config, **updates) if updates else config


def without_bundled_defaults(config: ExportConfig) -> ExportConfig:
    """Do not persist app-location defaults when the user moves a portable ZIP."""
    defaults = _defaults()
    updates = {name: None for name, value in defaults.items() if getattr(config, name) == value}
    return dataclasses.replace(config, **updates) if updates else config


def runtime_report(config: ExportConfig | None = None) -> dict:
    """Machine-readable capability discovery: no key-cache or WeChat access."""
    original = config or ExportConfig()
    effective = apply_bundled_defaults(original)
    defaults = _defaults()
    bundle = bundled_runtime_dir()
    tools = {}
    for field, label in _TOOL_NAMES.items():
        path = getattr(effective, field)
        origin = "configured" if getattr(original, field) is not None else "missing"
        if path is None and field != "silk_module":
            found = shutil.which("wxmori-media" if field == "capture_exe" else label)
            path = Path(found).resolve() if found else None
            if path is not None:
                origin = "path"
        if path is not None and path == defaults.get(field):
            origin = "bundled" if bundle is not None else "source"
        available = _file(path)
        tools[label] = {"available": available, "path": str(path) if path else None,
                        "bundled": origin == "bundled", "origin": origin}
    try:
        tkinter = importlib.util.find_spec("tkinter") is not None
    except (ValueError, ImportError):
        tkinter = False
    windows = sys.platform == "win32"
    architecture = platform.machine().lower()
    return {
        "schema_version": 1,
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": "windows-x64" if windows and architecture in ("amd64", "x86_64")
                    else f"{sys.platform}-{architecture}",
        "tools": tools,
        "capabilities": {
            "gui": tkinter,
            "json_export": True,
            "offline_html": True,
            "capture": windows and tools["capture"]["available"],
            "voice_decode": tools["node"]["available"] and tools["silk"]["available"],
            "video_transcode": tools["ffmpeg"]["available"],
            "media_probe": tools["ffprobe"]["available"],
        },
        "notes": ["Availability does not imply compatible WeChat databases or loaded keys.",
                  "No accounts, databases or key caches are inspected by doctor."],
    }
