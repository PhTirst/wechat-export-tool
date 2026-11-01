"""Build-time runtime preparation tests; no account files or network access."""
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_windows_runtime.py"
SPEC = importlib.util.spec_from_file_location("runtime_preparation", SCRIPT)
preparation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preparation)


def test_module_inventory_uses_only_actual_linked_packages():
    module = {"Path": "example.test/actual", "Version": "v1.0.0", "Dir": "synthetic"}
    output = "\n".join(json.dumps(value) for value in [
        {"ImportPath": "fmt"}, {"Module": module}, {"Module": module}])
    with patch.object(preparation.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)) as run:
        result = preparation.modules(Path("synthetic-source"))
    assert result == [module]
    assert run.call_args.args[0] == ["go", "list", "-deps", "-json", "./cmd/wxmori"]
    assert run.call_args.kwargs["env"]["GOOS"] == "windows"
    assert run.call_args.kwargs["env"]["GOARCH"] == "amd64"


def test_missing_module_directory_fails_before_any_recursive_copy():
    with tempfile.TemporaryDirectory(prefix="synthetic-runtime-source-") as name:
        source = Path(name)
        (source / "go.mod").write_text("module example.test/synthetic\n", encoding="utf-8")
        with patch.object(preparation, "modules", return_value=[{"Path": "example.test/missing"}]), \
             patch.object(Path, "rglob", side_effect=AssertionError("must not inspect cwd")):
            with pytest.raises(ValueError, match="capture_module_source_directory_missing"):
                preparation.capture_licenses(source, source / "output", set())


def test_cached_download_must_match_pin_without_network():
    with tempfile.TemporaryDirectory(prefix="synthetic-runtime-download-") as name:
        target = Path(name) / "cached.zip"
        target.write_bytes(b"synthetic")
        with patch.object(preparation.urllib.request, "urlopen", side_effect=AssertionError("no network")):
            with pytest.raises(ValueError, match="runtime_cached_download_digest_mismatch"):
                preparation.download("https://example.invalid/pinned.zip", target,
                                     algorithm="sha256", expected="0" * 64)


def test_selected_file_rejects_escape_before_writing():
    with tempfile.TemporaryDirectory(prefix="synthetic-runtime-path-") as name:
        output = Path(name) / "output"
        output.mkdir()
        with pytest.raises(ValueError, match="runtime_output_path_invalid"):
            preparation.write_selected(output, "../outside.txt", b"synthetic", set())
        assert not (output.parent / "outside.txt").exists()


def test_openssl_version_uses_linked_runtime_text_not_packed_tuple():
    with patch.object(preparation.ssl, "OPENSSL_VERSION", "OpenSSL 3.0.16 11 Feb 2025"), \
         patch.object(preparation.ssl, "OPENSSL_VERSION_INFO", (3, 0, 0, 16, 0)):
        assert preparation.python_openssl_version() == "3.0.16"
