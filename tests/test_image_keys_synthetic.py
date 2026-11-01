"""All process APIs, cache loading, and DPAPI are faked; no live scanner runs."""
import ctypes
import io
import json
import os
import struct
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from wechat_export_tool import image_keys as keys
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
    def __call__(self, *args):
        return self.callback(*args)


class FakeKernel:
    def __init__(self, key):
        self.key = key
        self.read_pids = []
        self.closed = []
        self.OpenProcess = FakeFunction(lambda access, inherit, pid: pid)
        self.CloseHandle = FakeFunction(lambda handle: self.closed.append(handle) or 1)
        self.VirtualQueryEx = FakeFunction(self.query)
        self.ReadProcessMemory = FakeFunction(self.read)
    def query(self, handle, address, result, structure_size):
        if address >= 0x1000 + (1 << 30):
            return 0
        region = result._obj
        region.BaseAddress = 0x1000
        region.RegionSize = 1 << 30  # Must not skip regions >= 512 MiB.
        region.State = 0x1000
        region.Protect = 0x204      # READWRITE | NOCACHE is still readable.
        return structure_size
    def read(self, handle, address, buffer, amount, count):
        self.read_pids.append(handle)
        data = b"\0" + self.key.decode().encode("utf-16le") + b"\0\0" if handle == 303 else b"\0" * 100
        ctypes.memmove(buffer, data, len(data))
        count._obj.value = len(data)
        return 1


class ImageKeySyntheticTests(unittest.TestCase):
    def synthetic_dat(self, directory, key=b"syntheticKey1234", xor=0x42):
        payload = b"\x89PNG\r\n\x1a\n" + b"synthetic PNG payload" + b"IEND\xaeB`\x82"
        dat = (keys.SIG + struct.pack("<II", 16, 8) + b"\0" +
               AES.new(key, AES.MODE_ECB).encrypt(pad(payload[:16], 16)) +
               payload[16:-8] + bytes(value ^ xor for value in payload[-8:]))
        path = Path(directory) / "synthetic.dat"
        path.write_bytes(dat)
        return path

    def test_ascii_utf16_and_binary_representations(self):
        key = b"syntheticKey1234"
        self.assertEqual(len(key), 16)
        for data in (b"\0" + key + b"\0", b"\0" + key.decode().encode("utf-16le") + b"\0\0",
                     b"\0" + key.hex().encode() + b"\0",
                     b"\0" + key.hex().encode().decode().encode("utf-16le") + b"\0\0",
                     b"\0" + key + b"suffix" + b"\0"):
            self.assertIn(key, set(keys.image_key_candidates(data)))

    def test_guard_pages_are_rejected_and_modifier_flags_allowed(self):
        self.assertTrue(keys._readable_region(0x1000, 0x204))
        self.assertFalse(keys._readable_region(0x1000, 0x104))
        self.assertFalse(keys._readable_region(0x1000, 0x01))
        self.assertFalse(keys._readable_region(0x2000, 0x04))

    def test_candidate_pipeline_includes_expanded_binary_schedule(self):
        from wechat_export_tool.aes128_schedule import expand_aes128_key
        key = bytes(range(16))
        self.assertIn(key, set(keys.image_key_candidates(b"\x99" * 9 + expand_aes128_key(key))))

    def test_newest_distinct_sample_order(self):
        with tempfile.TemporaryDirectory(prefix="synthetic_image_key_samples_") as temporary:
            root = Path(temporary)
            paths = []
            for name, stamp in (("a_t.dat", 40), ("a_W.dat", 30), ("z_W.dat", 20)):
                path = root / name
                path.write_bytes(keys.SIG + struct.pack("<II", 16, 8) + b"\0" + b"\0" * 32)
                os.utime(path, (stamp, stamp))
                paths.append(path)
            self.assertEqual([path.name for path, _ in keys._sample_files(paths)], ["a_t.dat", "z_W.dat"])

    def test_synthetic_poll_reenumerates_fairly_and_accepts_large_region_utf16(self):
        key = b"syntheticKey1234"
        png = b"\x89PNG\r\n\x1a\n" + b"synthetic PNG body payload" + b"IEND\xaeB`\x82"
        xor = 0x42
        dat = (keys.SIG + struct.pack("<II", 16, 8) + b"\0" +
               AES.new(key, AES.MODE_ECB).encrypt(pad(png[:16], 16)) +
               png[16:-8] + bytes(value ^ xor for value in png[-8:]))
        kernel, saved, clock = FakeKernel(key), io.StringIO(), [0.0]
        events = []
        enumerations = []
        def pids():
            enumerations.append(True)
            return [101] if len(enumerations) == 1 else [101, 303]
        def now():
            clock[0] += 0.003
            return clock[0]
        with tempfile.TemporaryDirectory(prefix="synthetic_image_key_scan_") as temporary:
            sample = Path(temporary) / "sample.dat"
            account, cache = Path(temporary) / "account", Path(temporary) / "cache.json"
            sample.write_bytes(dat)
            with patch.object(keys, "target_files", return_value=[sample]), \
                 patch.object(keys, "load", return_value=(None, None)), \
                 patch.object(keys.ctypes, "WinDLL", return_value=kernel, create=True), \
                 patch.object(keys, "_capture_supported", return_value=True), \
                 patch.object(keys, "_current_user_owns_pid", return_value=True), \
                 patch.object(keys.wi, "_get_wechat_image_pids", side_effect=pids), \
                 patch.object(keys.time, "monotonic", side_effect=now), \
                 patch.object(keys.time, "sleep"), \
                 patch.object(keys, "protect_bytes", return_value="SYNTHETIC-DPAPI"), \
                 patch.object(keys, "private_text_writer", return_value=nullcontext(saved)):
                actual_key, actual_xor = keys.scan(account_root=account, conversation_id="synthetic",
                    cache_file=cache, timeout=1, progress_cb=events.append)
        self.assertEqual((actual_key, actual_xor), (key, xor))
        self.assertGreaterEqual(len(enumerations), 2)
        self.assertIn(101, kernel.read_pids)
        self.assertIn(303, kernel.read_pids)
        self.assertEqual(kernel.read_pids.count(101), 2)
        self.assertTrue(kernel.closed)
        self.assertEqual(json.loads(saved.getvalue())["format"], keys.CACHE_FORMAT)
        self.assertEqual(events[-1]["found"], True)

    def test_dpapi_envelope_read_zeroes_temporary_plaintext(self):
        key = b"syntheticKey1234"
        with tempfile.TemporaryDirectory(prefix="synthetic_image_key_envelope_") as temporary:
            account = Path(temporary) / "account"
            binding = keys.account_fingerprint(account)
            temporary_plaintext = bytearray(json.dumps({"aes": key.hex(), "xor": 66, "account_hash": binding}).encode())
            path = Path(temporary) / "candidate.json"
            path.write_text(json.dumps({"format": keys.CACHE_FORMAT, "account_hash": binding, "protected": "FAKE"}))
            with patch.object(keys, "unprotect_bytes", return_value=temporary_plaintext):
                self.assertEqual(keys._load_envelope(path, account_root=account), (key, 66))
        self.assertFalse(any(temporary_plaintext))

    def test_accept_candidate_validates_and_reinfers_xor(self):
        key = b"syntheticKey1234"
        with tempfile.TemporaryDirectory(prefix="synthetic_image_key_accept_") as temporary:
            source = self.synthetic_dat(temporary)
            account, cache = Path(temporary) / "account", Path(temporary) / "cache.json"
            events = []
            with patch.object(keys, "_load_envelope", return_value=(key, 255)), \
                 patch.object(keys, "target_files", return_value=[source]), \
                 patch.object(keys, "_save_key") as save:
                self.assertEqual(keys.accept_candidate(Path("unused.synthetic.json"), account_root=account,
                    conversation_id="synthetic", cache_file=cache, progress_cb=events.append), (key, 66))
            save.assert_called_once_with(key, 66, cache, account_root=account)
            self.assertEqual(events[-1]["accepted"], True)
            self.assertEqual(events[-1]["xor_verified_samples"], 1)
            with patch.object(keys, "_load_envelope", return_value=(b"otherFakeKey1234", None)), \
                 patch.object(keys, "target_files", return_value=[source]), \
                 patch.object(keys, "_save_key") as save:
                self.assertEqual(keys.accept_candidate(Path("unused.synthetic.json"), account_root=account,
                    conversation_id="synthetic", cache_file=cache), (None, None))
            save.assert_not_called()

    def test_valid_cached_aes_without_xor_is_repaired_without_memory_access(self):
        key = b"syntheticKey1234"
        with tempfile.TemporaryDirectory(prefix="synthetic_image_key_repair_") as temporary:
            source = self.synthetic_dat(temporary)
            account, cache = Path(temporary) / "account", Path(temporary) / "cache.json"
            with patch.object(keys, "target_files", return_value=[source]), \
                 patch.object(keys, "load", return_value=(key, None)), \
                 patch.object(keys, "_save_key") as save, \
                 patch.object(keys.ctypes, "WinDLL", side_effect=AssertionError("No process API may run"), create=True):
                self.assertEqual(keys.scan(account_root=account, conversation_id="synthetic", cache_file=cache), (key, 66))
            save.assert_called_once_with(key, 66, cache, account_root=account)

    def test_account_cache_isolation_and_load_has_no_process_access(self):
        with tempfile.TemporaryDirectory(prefix="synthetic_image_key_accounts_") as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            self.assertNotEqual(keys.cache_path(root / "cache", first), keys.cache_path(root / "cache", second))
            binding = keys.account_fingerprint(first)
            cache = root / "candidate.json"
            cache.write_text(json.dumps({"format": keys.CACHE_FORMAT, "account_hash": binding, "protected": "FAKE"}))
            with patch.object(keys, "unprotect_bytes", side_effect=AssertionError("mismatch must fail before DPAPI")), \
                 patch.object(keys.ctypes, "WinDLL", side_effect=AssertionError("no process access"), create=True):
                with self.assertRaisesRegex(ValueError, "account_mismatch"):
                    keys.load(cache, account_root=second)

    def test_target_files_never_include_other_conversations(self):
        import hashlib
        with tempfile.TemporaryDirectory(prefix="synthetic_image_key_scope_") as temporary:
            account = Path(temporary)
            for conversation in ("chosen", "other"):
                folder = account / "msg" / "attach" / hashlib.md5(conversation.encode()).hexdigest()
                folder.mkdir(parents=True)
                (folder / "sample.dat").write_bytes(b"synthetic")
            result = keys.target_files(account, "chosen")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].parent.name, hashlib.md5(b"chosen").hexdigest())


if __name__ == "__main__":
    unittest.main()
