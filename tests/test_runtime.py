from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from wechat_export_tool.config import ExportConfig
from wechat_export_tool.runtime import (
    apply_bundled_defaults, bundled_runtime_dir, runtime_report, without_bundled_defaults,
)


def bundle(tmp_path):
    root = tmp_path / "portable"
    for name in ("capture/wxmori-media.exe", "node/node.exe", "silk/lib/index.cjs",
                 "silk/lib/silk.wasm", "ffmpeg/ffmpeg.exe", "ffmpeg/ffprobe.exe"):
        path = root / "runtime" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    return root


def test_frozen_tools_are_independent_of_working_directory(tmp_path, monkeypatch):
    root = bundle(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(root / "wechat-export.exe"))
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    config = apply_bundled_defaults(ExportConfig())
    assert bundled_runtime_dir() == root / "runtime"
    assert config.capture_exe == root / "runtime/capture/wxmori-media.exe"
    assert config.silk_module == root / "runtime/silk/lib/index.cjs"
    report = runtime_report()
    assert report["frozen"]
    assert all(tool["available"] and tool["bundled"] for tool in report["tools"].values())
    assert all(tool["bundled"] for tool in runtime_report(config)["tools"].values())


def test_explicit_overrides_are_preserved_even_when_missing(tmp_path, monkeypatch):
    root = bundle(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(root / "wechat-export.exe"))
    custom = tmp_path / "custom/missing-node.exe"
    config = ExportConfig(node=custom)
    assert apply_bundled_defaults(config).node == custom
    assert not runtime_report(config)["tools"]["node"]["available"]


def test_missing_frozen_tool_keeps_expected_location_without_path_fallback(tmp_path, monkeypatch):
    root = tmp_path / "portable"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(root / "wechat-export.exe"))
    with patch("wechat_export_tool.runtime.shutil.which", side_effect=AssertionError("PATH fallback")):
        config = apply_bundled_defaults(ExportConfig())
        report = runtime_report()
    assert config.capture_exe == root / "runtime/capture/wxmori-media.exe"
    assert not report["tools"]["capture"]["available"]
    assert not report["capabilities"]["voice_decode"]


def test_flat_silk_and_internal_runtime_are_supported(tmp_path, monkeypatch):
    unpacked = tmp_path / "_internal"
    silk = unpacked / "runtime/silk/index.cjs"
    silk.parent.mkdir(parents=True)
    silk.write_text("synthetic")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "portable/wechat-export.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(unpacked), raising=False)
    assert apply_bundled_defaults(ExportConfig()).silk_module == silk


def test_persisting_config_removes_only_portable_defaults(tmp_path, monkeypatch):
    root = bundle(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(root / "wechat-export.exe"))
    original = ExportConfig(node=tmp_path / "custom-node.exe", account_id="demo")
    effective = apply_bundled_defaults(original)
    assert without_bundled_defaults(effective) == original


def test_doctor_does_not_import_or_scan_account_engine(tmp_path):
    with patch.dict(sys.modules, {"wechat_export_tool.engine": None,
                                  "wechat_export_tool.backend": None}), \
         patch("wechat_export_tool.runtime.shutil.which", return_value=None):
        report = runtime_report(ExportConfig(data_root=tmp_path / "no-account"))
    assert report["schema_version"] == 1
    assert report["version"] == "0.2.0"
    assert "accounts" not in report
