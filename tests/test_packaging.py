"""Synthetic tests for the Windows release's explicit publication boundary."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "packaging/build_support.py"
SPEC = importlib.util.spec_from_file_location("wechat_bundle_build_support", SOURCE)
support = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(support)


def runtime(root):
    values = {"schema_version": 1, "platform": "win-x64", "selected_files": [], "file_sha256": {}}
    for relative in ("node/node.exe", "node/LICENSE", "silk/LICENSE", "silk/lib/index.cjs", "silk/lib/silk.wasm"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic runtime artifact")
        values["selected_files"].append(relative)
        values["file_sha256"][relative] = support.sha256(path)
    (root / "runtime-manifest.json").write_text(json.dumps(values), encoding="utf-8")
    return values


def test_runtime_manifest_ignores_download_cache_and_rejects_tampering(tmp_path):
    values = runtime(tmp_path)
    cache = tmp_path / ".downloads" / "not-for-publication.db"
    cache.parent.mkdir()
    cache.write_bytes(b"unselected synthetic cache")
    _, selected = support.runtime_inputs(tmp_path)
    assert {relative for _, relative in selected} == set(values["selected_files"])
    (tmp_path / "node/node.exe").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash_mismatch"):
        support.runtime_inputs(tmp_path)


@pytest.mark.parametrize("relative", ["../outside.exe", "node/../../outside.exe", "node\\outside.exe", "C:/outside.exe", ".downloads/archive.zip", "node/account.db-wal"])
def test_runtime_manifest_rejects_outside_or_private_inputs(tmp_path, relative):
    values = runtime(tmp_path)
    values["selected_files"].append(relative)
    values["file_sha256"][relative] = "0" * 64
    (tmp_path / "runtime-manifest.json").write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(ValueError):
        support.runtime_inputs(tmp_path)


def test_runtime_manifest_requires_the_actual_wasm_payload(tmp_path):
    values = runtime(tmp_path)
    values["selected_files"].remove("silk/lib/silk.wasm")
    (tmp_path / "runtime-manifest.json").write_text(json.dumps(values), encoding="utf-8")
    with pytest.raises(ValueError, match="required_files_missing"):
        support.runtime_inputs(tmp_path)


def test_metadata_is_rebuilt_without_editable_install_paths(tmp_path):
    requested = []
    def read_text(name):
        requested.append(name)
        return {"METADATA": "Name: synthetic\nVersion: 1.0\n", "WHEEL": "Wheel-Version: 1.0\n"}.get(name)
    distribution = SimpleNamespace(metadata={"Name": "synthetic"}, version="1.0", read_text=read_text)
    with patch.object(support, "DISTRIBUTIONS", ("synthetic",)), patch.object(support.metadata, "distribution", return_value=distribution):
        copied = support.prepare_metadata(tmp_path)
    assert set(requested) == set(support.METADATA_FILES)
    assert {Path(path).name for path, _ in copied} == {"METADATA", "WHEEL"}
    assert not list(tmp_path.rglob("direct_url.json"))


def test_metadata_auto_collected_by_pyinstaller_is_filtered_too():
    entries = [("example-1.0.dist-info/" + name, "synthetic-source", "DATA") for name in
               ("METADATA", "direct_url.json", "RECORD", "INSTALLER", "REQUESTED", "licenses/LICENSE")]
    result = support.publishable_datas(entries)
    assert [item[0] for item in result] == ["example-1.0.dist-info/METADATA", "example-1.0.dist-info/licenses/LICENSE"]


@pytest.mark.parametrize("name", ["account.db", "account.db-wal", "secret.key", "cached.dpapi.json", "direct_url.json", ".private/hidden.txt", "example.dist-info/RECORD"])
def test_bundle_inventory_rejects_private_outputs(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic must not publish")
    with pytest.raises(ValueError):
        support.inventory_bundle(tmp_path)


def test_final_archive_has_inventory_and_relative_paths(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "wechat-export.exe").write_bytes(b"MZ synthetic launcher")
    output = tmp_path / "output"
    artifact = support.finalize(bundle, output)
    with zipfile.ZipFile(artifact) as archive:
        assert set(archive.namelist()) == {"WeChatExportTool/wechat-export.exe", "WeChatExportTool/bundle-manifest.json"}
        manifest = json.loads(archive.read("WeChatExportTool/bundle-manifest.json"))
        assert manifest["files"][0]["path"] == "wechat-export.exe"
        assert manifest["files"][0]["sha256"] == support.sha256(bundle / "wechat-export.exe")
    assert (output / "SHA256SUMS.txt").read_text(encoding="ascii").startswith(support.sha256(artifact) + "  ")
