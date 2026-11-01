"""Synthetic keyring, capture-supervision and storage/security regressions.

All paths are test-owned temporary directories. Popen is always mocked; no
WeChat process, existing cache, personal database or account data is inspected.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import signal
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wechat_export_tool import backend, security, storage
from wechat_export_tool.config import ExportConfig
from wechat_export_tool.errors import ExportError


def config_for(root: Path) -> ExportConfig:
    return ExportConfig(data_root=root / "synthetic-data", account_id="synthetic-account",
                        cache_dir=root / "synthetic-cache", capture_exe=root / "synthetic-capture.exe")


class KeyringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-keyring-")
        self.root = Path(self.temp.name)
        self.config = config_for(self.root)
        self.salt, self.key = bytes(range(16)), bytes(range(32))
        self.envelope = backend.keyring_path(self.config)
        self.envelope.parent.mkdir(parents=True)
        self.envelope.write_text(json.dumps({"format": "wxmori.dpapi.v1",
                                             "protected": "synthetic-envelope"}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **overrides):
        material = {"version": 1, "account": backend.account_fingerprint(self.config),
                    "database_keys": {self.salt.hex(): self.key.hex()}}
        material.update(overrides)
        return bytearray(json.dumps(material).encode("utf-8"))

    def test_account_binding_and_dpapi_buffer_are_checked(self):
        payload = self.payload()
        with patch.object(backend, "unprotect_bytes", return_value=payload) as unprotect:
            self.assertEqual(backend.load_page_keys(self.config), {self.salt: self.key})
        unprotect.assert_called_once_with("synthetic-envelope")
        self.assertFalse(any(payload))
        expected = hashlib.sha256(os.path.normpath(os.path.abspath(
            self.config.account_root / "db_storage")).lower().encode("utf-8")).hexdigest()
        self.assertEqual(backend.account_fingerprint(self.config), expected)
        other = ExportConfig(data_root=self.config.data_root, account_id="second-synthetic",
                             cache_dir=self.config.cache_dir)
        self.assertNotEqual(backend.keyring_path(other), backend.keyring_path(self.config))

    def test_wrong_account_or_version_is_rejected_and_buffer_wiped(self):
        for overrides in ({"account": "another-synthetic-account"}, {"version": 99}):
            payload = self.payload(**overrides)
            with patch.object(backend, "unprotect_bytes", return_value=payload):
                with self.assertRaises(ExportError):
                    backend.load_page_keys(self.config)
            self.assertFalse(any(payload))

    def test_salt_and_key_lengths_and_types_are_strict(self):
        for values in ({"ab" * 15: self.key.hex()}, {"zz" * 16: self.key.hex()},
                       {self.salt.hex(): "ab" * 31}, {self.salt.hex(): 123},
                       ["not a mapping"]):
            payload = self.payload(database_keys=values)
            with patch.object(backend, "unprotect_bytes", return_value=payload):
                with self.assertRaises(ExportError):
                    backend.load_page_keys(self.config)
            self.assertFalse(any(payload))

    def test_nonobject_material_and_invalid_envelope_fail_with_safe_errors(self):
        with patch.object(backend, "unprotect_bytes", return_value=bytearray(b"[]")):
            with self.assertRaises(ExportError):
                backend.load_page_keys(self.config)
        self.envelope.write_text(json.dumps({"format": "unsupported"}), encoding="utf-8")
        with patch.object(backend, "unprotect_bytes") as unprotect:
            with self.assertRaises(ExportError):
                backend.load_page_keys(self.config)
            unprotect.assert_not_called()

    def test_missing_and_oversize_envelopes(self):
        self.envelope.unlink()
        with patch.object(backend, "unprotect_bytes") as unprotect:
            self.assertEqual(backend.load_page_keys(self.config), {})
            unprotect.assert_not_called()
        self.envelope.write_bytes(b"x" * ((1 << 20) + 1))
        with self.assertRaisesRegex(ExportError, "^local_json_too_large$"):
            backend.load_page_keys(self.config)


class ProbeTests(unittest.TestCase):
    @staticmethod
    def write_sample(path: Path, raw_size: int, *, ciphertext: bytes | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        padded = raw_size + 16 - raw_size % 16
        header = b"\x07\x08V2\x08\x07" + struct.pack("<II", raw_size, 0) + b"\x01"
        path.write_bytes(header + (ciphertext if ciphertext is not None else bytes(padded)))

    def test_only_selected_chat_and_complete_pkcs7_ciphertext_are_probed(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-image-probe-") as temp:
            root = Path(temp)
            config = config_for(root)
            selected, other = "synthetic-selected", "synthetic-other"
            selected_dir = config.account_root / "msg/attach" / hashlib.md5(selected.encode()).hexdigest()
            other_dir = config.account_root / "msg/attach" / hashlib.md5(other.encode()).hexdigest()
            self.write_sample(selected_dir / "2026-01/Img/a.dat", 32, ciphertext=b"A" * 48)
            self.write_sample(selected_dir / "2026-01/Img/b.dat", 17, ciphertext=b"B" * 32)
            self.write_sample(other_dir / "2026-01/Img/c.dat", 16, ciphertext=b"C" * 32)
            output = backend.prepare_image_probe(config, selected)
            probe = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(probe["version"], 1)
            payloads = {base64.b64decode(sample["ciphertext_base64"]) for sample in probe["samples"]}
            self.assertEqual(payloads, {b"A" * 48, b"B" * 32})
            self.assertEqual(output.parent, backend.account_cache(config))

    def test_probe_is_bounded_to_eight_and_rejects_empty_large_truncated_segments(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-image-probe-bounds-") as temp:
            root = Path(temp)
            config = config_for(root)
            samples = []
            for number in range(10):
                path = root / f"valid-{number}.dat"
                self.write_sample(path, 16)
                samples.append(path)
            with patch("wechat_export_tool.image_keys.target_files", return_value=samples) as files:
                probe = backend.prepare_image_probe(config, "selected")
            files.assert_called_once_with(config.account_root, "selected")
            self.assertEqual(len(json.loads(probe.read_text(encoding="utf-8"))["samples"]), 8)
            invalid = []
            for name, size, data in (("empty", 0, bytes(16)),
                                     ("oversize", 2 << 20, b"x" * 32),
                                     ("truncated", 32, b"x" * 32)):
                path = root / (name + ".dat")
                self.write_sample(path, size, ciphertext=data)
                invalid.append(path)
            with patch("wechat_export_tool.image_keys.target_files", return_value=invalid):
                with self.assertRaisesRegex(ExportError, "^no_local_v2_image_for_selected_conversation$"):
                    backend.prepare_image_probe(config, "selected")


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-capture-supervision-")
        self.root = Path(self.temp.name)
        self.config = config_for(self.root)
        (self.config.account_root / "db_storage").mkdir(parents=True)
        self.config.capture_exe.write_bytes(b"synthetic non-executable placeholder")
        self.os_mock = SimpleNamespace(name="nt", path=os.path, environ={
            "PATH": "synthetic-path", "WXMORI_MASTER_KEY": "SECRET_SENTINEL",
            "wxmori_capture_pid": "999999", "WXMORI_IMAGE_PROBE": "old-probe",
            "NORMAL_ENVIRONMENT": "preserved"})

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, process, **kwargs):
        output = io.StringIO()
        with patch.object(backend, "os", self.os_mock), \
             patch.object(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
             patch.object(signal, "CTRL_BREAK_EVENT", 1, create=True), \
             patch.object(backend.subprocess, "Popen", return_value=process) as spawn, \
             contextlib.redirect_stdout(output):
            result = backend.capture_keys(self.config, **kwargs)
        return result, output.getvalue(), spawn

    @staticmethod
    def process(lines=(), returncode=0):
        return SimpleNamespace(stdout=io.StringIO("\n".join(lines)),
                               wait=Mock(return_value=returncode), send_signal=Mock(),
                               kill=Mock(), terminate=Mock())

    def test_command_explicit_parameters_clean_environment_and_partial_exit(self):
        event = {"state": "partial", "verified_databases": 1, "required_databases": 2,
                 "running": False, "master_key": "SECRET_SENTINEL", "path": "PRIVATE_PATH_SENTINEL"}
        process = self.process(["not-json SECRET_SENTINEL", json.dumps(event)], returncode=2)
        probe = self.root / "explicit-probe.json"
        probe.write_text("{}", encoding="utf-8")
        result, output, spawn = self.invoke(process, timeout=19, pid=456, images_probe=probe)
        self.assertEqual(result["returncode"], 2)
        self.assertEqual(result["status"], "partial")
        command = spawn.call_args.args[0]
        self.assertEqual(command, [str(self.config.capture_exe.resolve()), "--config",
            str(backend.capture_config_path(self.config)), "key", "capture", "--timeout", "19",
            "--mode", "auto", "--progress", "json"])
        options = spawn.call_args.kwargs
        self.assertEqual(options["creationflags"], 512)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["env"]["NORMAL_ENVIRONMENT"], "preserved")
        self.assertEqual({key for key in options["env"] if key.upper().startswith("WXMORI_")},
                         {"WXMORI_CAPTURE_PID", "WXMORI_IMAGE_PROBE", "WXMORI_IMAGE_KEY_FILE",
                          "WXMORI_STRICT_ACCOUNT_ONLY"})
        self.assertEqual(options["env"]["WXMORI_STRICT_ACCOUNT_ONLY"], "1")
        self.assertEqual(options["env"]["WXMORI_CAPTURE_PID"], "456")
        self.assertEqual(options["env"]["WXMORI_IMAGE_PROBE"], str(probe.resolve()))
        self.assertNotIn("SECRET_SENTINEL", output + json.dumps(result))
        self.assertNotIn("PRIVATE_PATH_SENTINEL", output + json.dumps(result))
        generated = json.loads(backend.capture_config_path(self.config).read_text(encoding="utf-8"))
        self.assertEqual(generated["data_dir"], str((self.config.account_root / "db_storage").absolute()))
        self.assertTrue(process.stdout.closed)
        process.send_signal.assert_not_called()

    def test_progress_values_cannot_smuggle_secrets_through_allowed_fields(self):
        malicious = {"state": "SECRET_SENTINEL", "verified_databases": "SECRET_SENTINEL",
                     "ready_for_login": {"key": "SECRET_SENTINEL"}, "required_databases": -5}
        process = self.process([json.dumps(malicious)])
        result, output, _ = self.invoke(process)
        self.assertNotIn("SECRET_SENTINEL", output + json.dumps(result))
        self.assertIs(type(result["verified_databases"]), int)
        self.assertGreaterEqual(result["verified_databases"], 0)

    def test_progress_nonstring_state_cannot_abort_capture_supervision(self):
        for state in ({"key": "SECRET_SENTINEL"}, ["SECRET_SENTINEL"], None):
            process = self.process([json.dumps({"state": state, "ready_for_login": True})])
            result, output, _ = self.invoke(process)
            self.assertNotIn("SECRET_SENTINEL", output + json.dumps(result))
            process.wait.assert_called_once_with(timeout=20)

    def test_keyboard_interrupt_requests_graceful_detach_and_waits(self):
        class InterruptedStream:
            closed = False

            def __iter__(self):
                raise KeyboardInterrupt()

            def close(self):
                self.closed = True

        process = self.process()
        process.stdout = InterruptedStream()
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(process)
        process.send_signal.assert_called_once_with(1)
        process.wait.assert_called_once_with(timeout=30)
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.assertTrue(process.stdout.closed)

    def test_invalid_timeout_pid_and_missing_probe_do_not_spawn(self):
        for kwargs in ({"timeout": 0}, {"timeout": 601}, {"timeout": True},
                       {"pid": 0}, {"pid": -1}, {"pid": True},
                       {"images_probe": self.root / "missing-probe"}):
            process = self.process()
            with patch.object(backend, "os", self.os_mock), \
                 patch.object(backend.subprocess, "Popen") as spawn:
                with self.assertRaises(ExportError):
                    backend.capture_keys(self.config, **kwargs)
                spawn.assert_not_called()


class SecurityAndStorageTests(unittest.TestCase):
    def test_dpapi_wrappers_mock_envelopes_and_wipe_intermediate_buffer(self):
        encrypted = bytearray(b"synthetic cipher bytes")
        with patch.object(security, "_dpapi", return_value=encrypted) as protect:
            text = security.protect_bytes(b"synthetic plaintext")
        protect.assert_called_once_with(b"synthetic plaintext", decrypt=False)
        self.assertEqual(base64.b64decode(text), b"synthetic cipher bytes")
        self.assertFalse(any(encrypted))
        clear = bytearray(b"synthetic plaintext")
        with patch.object(security, "_dpapi", return_value=clear) as unprotect:
            self.assertIs(security.unprotect_bytes(text), clear)
        unprotect.assert_called_once_with(b"synthetic cipher bytes", decrypt=True)
        with patch.object(security, "_dpapi") as unprotect:
            for invalid in (None, "!not-base64!"):
                with self.assertRaises(ExportError):
                    security.unprotect_bytes(invalid)
            unprotect.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "DPAPI is a Windows user-bound primitive")
    def test_real_dpapi_roundtrip_with_synthetic_payload_only(self):
        payload = b"synthetic payload for DPAPI roundtrip only"
        envelope = security.protect_bytes(payload)
        decrypted = security.unprotect_bytes(envelope)
        try:
            self.assertEqual(decrypted, payload)
        finally:
            decrypted[:] = b"\0" * len(decrypted)

    def test_freeze_copies_whole_synthetic_families_and_cleans_private_scratch(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-storage-family-") as temp:
            root = Path(temp)
            sources = [root / "first.db", root / "second.db"]
            for number, source in enumerate(sources):
                source.write_bytes(bytes([number + 1]) * 4096)
                source.with_name(source.name + "-wal").write_bytes(b"synthetic wal")
            with storage.frozen_families(sources) as (scratch, copies):
                self.assertEqual(set(copies), set(sources))
                for source, copy in copies.items():
                    self.assertEqual(copy.read_bytes(), source.read_bytes())
                    self.assertEqual(copy.with_name(copy.name + "-wal").read_bytes(), b"synthetic wal")
                self.assertTrue(scratch.is_dir())
            self.assertFalse(scratch.exists())

    def test_readonly_snapshot_cannot_write_and_connection_is_closed(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-storage-readonly-") as temp:
            database = Path(temp) / "synthetic.db"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                with connection:
                    connection.execute("CREATE TABLE synthetic(value INTEGER)")
                    connection.execute("INSERT INTO synthetic VALUES(1)")
            with storage.readonly(database) as readonly:
                self.assertEqual(readonly.execute("SELECT value FROM synthetic").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute("INSERT INTO synthetic VALUES(2)")
            with self.assertRaises(sqlite3.ProgrammingError):
                readonly.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
