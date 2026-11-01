"""The release fixture uses valid encryption and only generated account data."""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from chatlog_keeper import wechat_db as wx

from wechat_export_tool.backend import load_page_keys


def script():
    path = Path(__file__).resolve().parents[1] / "scripts/smoke_windows_bundle.py"
    spec = importlib.util.spec_from_file_location("synthetic_bundle_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reserved_fixture_roundtrips_through_unmodified_upstream_decryptor(tmp_path):
    smoke = script()
    plain, encrypted, recovered = (tmp_path / name for name in ("plain.db", "encrypted.db", "recovered.db"))
    smoke.create_reserved_sqlite(plain, "CREATE TABLE example(value TEXT);INSERT INTO example VALUES('synthetic');")
    salt, key = smoke.encrypt_sqlite(plain, encrypted, label="unit-test")
    first = encrypted.read_bytes()[:4096]
    assert first[:16] == salt
    assert wx._verify_key_v4(key, first)
    assert wx._decrypt_db_v4_snapshot(encrypted, key, recovered)
    with closing(sqlite3.connect(recovered)) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM example").fetchone() == ("synthetic",)
    damaged = bytearray(encrypted.read_bytes())
    damaged[-1] ^= 1
    encrypted.write_bytes(damaged)
    rejected = tmp_path / "must-not-be-published.db"
    assert not wx._decrypt_db_v4_snapshot(encrypted, key, rejected)
    assert not rejected.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI fixture")
def test_fixture_keyring_and_account_remain_inside_explicit_synthetic_root(tmp_path):
    smoke = script()
    config = smoke.prepare_fixture(tmp_path, {"capabilities": {"voice_decode": False}})
    assert config.account_root.is_relative_to(tmp_path)
    assert config.cache_dir.is_relative_to(tmp_path)
    keys = load_page_keys(config)
    assert len(keys) == 2
    for relative in ("contact/contact.db", "message/message_0.db"):
        path = config.account_root / "db_storage" / relative
        first = path.read_bytes()[:4096]
        assert wx._verify_key_v4(keys[first[:16]], first)


def test_bundle_child_environment_has_only_windows_path_and_no_host_runtime_injection(monkeypatch):
    smoke = script()
    windows = Path("C:/Windows")
    monkeypatch.setenv("SystemRoot", str(windows))
    monkeypatch.setenv("PATH", os.pathsep.join(("D:/ExternalPython", "D:/ExternalNode", "D:/ExternalGo")))
    for name in ("PYTHONHOME", "PYTHONPATH", "GOROOT", "GOPATH", "GOBIN", "NODE_PATH", "NODE_OPTIONS",
                 "VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.setenv(name, "synthetic-host-runtime")
    before = dict(os.environ)
    isolated = smoke.child_environment(isolate_runtime=True)
    assert isolated["PATH"].split(os.pathsep) == [str(windows / "System32"), str(windows)]
    assert all(name not in isolated for name in ("PYTHONHOME", "PYTHONPATH", "GOROOT", "GOPATH", "GOBIN",
                                                "NODE_PATH", "NODE_OPTIONS", "VIRTUAL_ENV", "CONDA_PREFIX"))
    assert dict(os.environ) == before
    assert smoke.child_environment()["PATH"] == before["PATH"]


def test_command_helpers_forward_the_exact_isolated_environment(tmp_path, monkeypatch):
    smoke = script()
    isolated = {"PATH": "synthetic-system-only"}
    calls = []

    def run(arguments, **options):
        calls.append(options)
        return subprocess.CompletedProcess(arguments, 0, b'{"synthetic":true}', b"")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    smoke._run(["synthetic-absolute-cli"], cwd=tmp_path, environment=isolated)
    assert smoke._json_command(["synthetic-absolute-cli"], ["doctor", "--json"], tmp_path,
                               environment=isolated) == {"synthetic": True}
    assert len(calls) == 2 and all(item["env"] is isolated for item in calls)
