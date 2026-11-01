from pathlib import Path

import pytest

from wechat_export_tool.config import (
    ConfigError, ExportConfig, default_config_dir, default_config_path, load_config, save_config,
)


def test_default_settings_use_user_directory_and_have_no_personal_account(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    config = load_config(tmp_path / "missing.json")
    assert config.data_root == tmp_path / "home" / "xwechat_files"
    assert config.account_id is None
    assert config.cache_dir == tmp_path / "appdata" / "WeChatExportTool" / "cache"
    assert default_config_path() == default_config_dir() / "config.json"


def test_config_round_trip_keeps_paths_and_explicit_account(tmp_path):
    config = ExportConfig(data_root=tmp_path / "data", account_id="synthetic_account",
                          cache_dir=tmp_path / "cache", capture_exe=tmp_path / "capture.exe",
                          ffmpeg=tmp_path / "ffmpeg.exe", ffprobe=tmp_path / "ffprobe.exe",
                          node=tmp_path / "node.exe", silk_module=tmp_path / "silk")
    path = save_config(config, tmp_path / "settings" / "config.json")
    loaded = load_config(path)
    assert loaded == config
    assert loaded.account_root == config.data_root / "synthetic_account"
    assert isinstance(loaded.ffmpeg, Path)
    assert not list(path.parent.glob(".config-*.tmp"))
    assert "master_key" not in path.read_text(encoding="utf-8")


def test_relative_json_paths_resolve_beside_configuration(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"data_root":"data","cache_dir":"cache","capture_exe":"bin/backend.exe"}')
    config = load_config(path)
    assert config.data_root == tmp_path / "data"
    assert config.capture_exe == tmp_path / "bin" / "backend.exe"


@pytest.mark.parametrize("account", ["../another", "..", "a/b", "a\\b", "C:account", "name.", "name\n"])
def test_account_cannot_escape_data_root(account):
    with pytest.raises(ConfigError):
        ExportConfig(account_id=account)


@pytest.mark.parametrize("document", [
    {"master_key": "synthetic_secret"}, {"schema_version": 2}, {"schema_version": True},
    {"schema_version": 1.0}, {"data_root": None}, {"cache_dir": 12}, {"account_id": 12},
])
def test_rejects_unknown_secret_and_invalid_typed_configuration(document):
    with pytest.raises(ConfigError) as error:
        ExportConfig.from_dict(document)
    assert "synthetic_secret" not in str(error.value)


def test_invalid_json_error_does_not_echo_payload(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"secret":"synthetic_secret"', encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "synthetic_secret" not in str(error.value)


def test_unselected_account_requires_explicit_selection():
    with pytest.raises(ConfigError):
        _ = ExportConfig().account_root
