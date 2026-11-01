"""Synthetic-only voice tests; never inspect a real account or personal cache."""
from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import wave
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from wechat_export_tool import voice_attachments as voice


SILK = b"\x02#!SILK_V3" + bytes(range(32))


def make_database(path=None):
    connection = sqlite3.connect(str(path) if path else ":memory:")
    connection.executescript(
        "CREATE TABLE Name2Id(user_name TEXT);"
        "CREATE TABLE VoiceInfo(chat_name_id INTEGER,create_time INTEGER,local_id INTEGER,"
        "svr_id INTEGER,voice_data BLOB,data_index TEXT);"
        "INSERT INTO Name2Id VALUES('selected');INSERT INTO Name2Id VALUES('other');"
    )
    return connection


def message(server="123", **extra):
    value = {"msg_type": 34, "conversation_id": "selected", "server_id": server,
             "ts": 100, "local_id": 7}
    value.update(extra)
    return value


def write_wave(path: Path):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(bytes(4800))


class VoiceLookupTests(unittest.TestCase):
    def setUp(self):
        self.database = make_database()

    def tearDown(self):
        self.database.close()

    def insert(self, chat=1, timestamp=100, local=7, server=123, blob=SILK):
        self.database.execute("INSERT INTO VoiceInfo VALUES(?,?,?,?,?,0)",
                              (chat, timestamp, local, server, blob))

    def test_server_id_never_crosses_conversation_or_timestamp(self):
        self.insert(chat=2)
        self.assertIsNone(voice._voice_blob(self.database, "selected", message())[0])
        self.insert(timestamp=101)
        self.assertEqual(voice._voice_blob(self.database, "selected", message())[1],
                         "voice_identity_mismatch")
        self.insert(local=8)
        self.assertIsNone(voice._voice_blob(self.database, "selected", message())[0])
        self.insert()
        self.assertEqual(voice._voice_blob(self.database, "selected", message())[0], SILK)

    def test_missing_time_and_invalid_server_are_rejected(self):
        self.insert()
        for value in (None, float("nan"), float("inf"), -1):
            self.assertEqual(voice._voice_blob(self.database, "selected", message(ts=value))[1],
                             "voice_time_missing")
        for server in ("", "0", "bad", "9" * 40, str(2**63)):
            self.assertEqual(voice._voice_blob(self.database, "selected", message(server))[1],
                             "no_server_id")

    def test_ambiguous_or_invalid_blobs_fail_closed(self):
        self.insert(blob=b"not silk")
        self.assertEqual(voice._voice_blob(self.database, "selected", message())[1], "voice_blob_invalid")
        self.insert()
        self.insert(blob=SILK + b"different")
        self.assertEqual(voice._voice_blob(self.database, "selected", message())[1], "voice_identity_mismatch")

    def test_millisecond_time_and_optional_local_id(self):
        self.insert(timestamp=1700000000)
        source = message(ts=1700000000000)
        source.pop("local_id")
        self.assertEqual(voice._voice_blob(self.database, "selected", source)[0], SILK)


class VoiceCollectionTests(unittest.TestCase):
    def test_one_frozen_copy_set_and_one_batch_with_explicit_keys(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-voice-collect-") as temp:
            root = Path(temp)
            account, output, scratch = root / "account", root / "output", root / "scratch"
            media = account / "db_storage/message"
            media.mkdir(parents=True)
            scratch.mkdir()
            keys = {bytes([number]) * 16: bytes([number + 10]) * 32 for number in (1, 2)}
            plain_by_source = {}
            source_before = {}
            for number in (0, 1):
                source = media / f"media_{number}.db"
                source.write_bytes(bytes([number + 1]) * 16 + bytes(4080))
                source_before[source] = source.read_bytes()
                plain = root / f"synthetic-plain-{number}.db"
                db = make_database(plain)
                db.execute("INSERT INTO VoiceInfo VALUES(?,?,?,?,?,0)",
                           (2 if number == 0 else 1, 100, 7, 123, SILK))
                db.commit()
                db.close()
                plain_by_source[source] = plain

            @contextlib.contextmanager
            def freeze(sources):
                self.assertEqual(set(sources), set(plain_by_source))
                yield scratch, {source: source for source in sources}

            def decrypt(source, key, output_path):
                self.assertEqual(key, keys[source.read_bytes()[:16]])
                shutil.copyfile(plain_by_source[source], output_path)
                return True

            def batch(jobs, private_root, **kwargs):
                self.assertEqual(private_root, scratch)
                self.assertEqual(len(jobs), 1)
                results = {}
                for job in jobs:
                    self.assertEqual(Path(job["input"]).read_bytes(), SILK)
                    write_wave(Path(job["output"]))
                    results[job["id"]] = {"status": "available"}
                return results

            progress = []
            key_snapshot = dict(keys)
            with patch.object(voice, "frozen_families", side_effect=freeze) as frozen, \
                 patch.object(voice.wx, "_verify_key_v4", return_value=True), \
                 patch.object(voice.wx, "_decrypt_db_v4_snapshot", side_effect=decrypt), \
                 patch.object(voice, "_run_batch", side_effect=batch) as decoded:
                result = voice.collect_voice([message()], output, account_root=account,
                    page_keys=MappingProxyType(keys), progress_cb=progress.append)
            self.assertEqual(frozen.call_count, 1)
            self.assertEqual(decoded.call_count, 1)
            self.assertEqual(keys, key_snapshot)
            self.assertEqual(result["123"]["status"], "available")
            relative = Path(result["123"]["path"])
            self.assertEqual(relative.stem, hashlib.sha256(b"123").hexdigest()[:32])
            self.assertTrue(voice._verify_wave(output / relative))
            self.assertEqual({source: source.read_bytes() for source in source_before}, source_before)
            self.assertEqual(progress[-1]["stage"], "voice_complete")

    def test_scope_and_duplicate_identity_fail_without_source_access(self):
        with patch.object(voice, "frozen_families") as frozen:
            mixed = [message(), message("124", conversation_id="another")]
            result = voice.collect_voice(mixed, Path("unused"), account_root=Path("unused"), page_keys={})
            self.assertTrue(all(row["status"] == "missing" for row in result.values()))
            conflicting = [message(), message(ts=101)]
            result = voice.collect_voice(conflicting, Path("unused"), account_root=Path("unused"), page_keys={})
            self.assertEqual(result["123"]["reason"], voice._REASONS["voice_identity_mismatch"])
            frozen.assert_not_called()

    def test_absent_media_is_an_explicit_missing_result(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-voice-missing-") as temp:
            root = Path(temp)
            result = voice.collect_voice([message()], root / "output", account_root=root / "empty",
                                         page_keys={})
            self.assertEqual(result["123"]["reason"], voice._REASONS["media_database_missing"])


@unittest.skipUnless(shutil.which("node"), "Node is optional for Python-only tests")
class VoiceBatchTests(unittest.TestCase):
    def test_injected_module_one_batch_wav_and_duration_rejection(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-voice-node-") as temp:
            root = Path(temp)
            module = root / "synthetic-decoder.cjs"
            module.write_text("module.exports={decode:async function(){const b=Buffer.alloc(48004);"
                              "return {data:b.subarray(2,48002)}}};", encoding="utf-8")
            raw = root / "synthetic.silk"
            raw.write_bytes(SILK)
            jobs = [{"id": token, "input": str(raw), "output": str(root / (token + ".wav")),
                     "mp3": str(root / (token + ".mp3")), "expectedMs": expected}
                    for token, expected in (("valid", 1000), ("mismatch", 8000))]
            result = voice._run_batch(jobs, root, silk_module=module)
            self.assertEqual(result["valid"]["status"], "available")
            self.assertEqual(result["mismatch"]["reason"], "voice_duration_mismatch")
            self.assertTrue(voice._verify_wave(root / "valid.wav"))
            self.assertEqual((root / "valid.wav").stat().st_size, 48044)
            self.assertFalse((root / "mismatch.wav").exists())
            self.assertFalse((root / "valid.mp3").exists())
            manifest = json.loads((root / "voice-jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["silkModule"], str(module.resolve()))
            self.assertEqual(manifest["ffmpeg"], "")

    def test_explicit_missing_module_and_node_fail_cleanly(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-voice-runtime-") as temp:
            root = Path(temp)
            jobs = [{"id": "synthetic"}]
            result = voice._run_batch(jobs, root, silk_module=root / "absent.cjs")
            self.assertEqual(result["synthetic"]["reason"], "voice_decoder_missing")
            result = voice._run_batch(jobs, root, node=root / "absent-node")
            self.assertEqual(result["synthetic"]["reason"], "voice_decoder_missing")


if __name__ == "__main__":
    unittest.main()
