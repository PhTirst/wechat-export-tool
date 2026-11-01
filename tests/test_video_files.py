"""Synthetic-only video/file tests; never opens an account, export or keyring."""
from __future__ import annotations

import base64
import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_export_tool import video_file_attachments as media


class VideoFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic_wechat_video_")
        self.root = Path(self.temporary.name)
        self.account = self.root / "account"
        self.account.mkdir()
        self.output = self.root / "export"
        self.target = "synthetic-conversation"
        self.timestamp = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
        self.payload = b"synthetic bytes"
        media._encoder_options_cached.cache_clear()

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, relative, payload=None):
        path = self.account / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload if payload is None else payload)
        return path

    def message(self, sid=100, kind=49, meta=None):
        return {"server_id": sid, "conversation_id": self.target, "msg_type": kind,
                "ts": self.timestamp, "ts_iso": "2026-10-01T08:00:00+08:00",
                "attachment_meta": meta if meta is not None else {
                    "kind": "file", "filename": "synthetic.txt", "total_bytes": len(self.payload),
                    "md5": hashlib.md5(self.payload).hexdigest()}}

    def row(self, sid=100, kind=49, **values):
        return {"conversation_id": self.target, "source_shard": "message_0.db",
                "row": {"server_id": sid, "local_type": kind, "create_time": self.timestamp,
                        "local_id": 4, **values}}

    def collect(self, messages, rows, **options):
        return media.collect_video_files(messages, rows, self.output,
            account_root=self.account, page_keys={}, **options)

    def test_exact_month_checksum_and_legacy_raw_envelope(self):
        self.source("msg/file/2026-10/synthetic.txt")
        self.source("msg/file/2026-09/synthetic.txt", b"other month")
        raw = self.row()
        raw.pop("conversation_id")  # Backwards-compatible already-scoped JSONL.
        item = self.collect([self.message()], [raw])["100"][0]
        self.assertEqual(item["status"], "available")
        self.assertEqual((self.output / item["path"]).read_bytes(), self.payload)
        self.assertFalse(Path(item["path"]).is_absolute())
        self.assertNotIn("..", Path(item["path"]).parts)

    def test_wrong_month_does_not_match_same_filename(self):
        self.source("msg/file/2026-09/synthetic.txt")
        item = self.collect([self.message()], [self.row()])["100"][0]
        self.assertEqual(item["status"], "missing")

    def test_wrong_size_or_checksum_fails_closed(self):
        self.source("msg/file/2026-10/synthetic.txt")
        for field, value in (("total_bytes", len(self.payload) + 1), ("md5", "0" * 32)):
            message = self.message()
            message["attachment_meta"][field] = value
            self.assertEqual(self.collect([message], [self.row()])["100"][0]["status"], "missing")

    def test_path_traversal_and_alternate_stream_names_are_rejected(self):
        self.source("msg/file/2026-10/synthetic.txt")
        for name in ("../synthetic.txt", r"..\synthetic.txt", "synthetic.txt:stream", "/synthetic.txt"):
            message = self.message()
            message["attachment_meta"]["filename"] = name
            self.assertEqual(self.collect([message], [self.row()])["100"][0]["status"], "missing")

    def test_explicit_other_conversation_raw_is_rejected(self):
        self.source("msg/file/2026-10/synthetic.txt")
        raw = self.row()
        raw["conversation_id"] = "other-conversation"
        item = self.collect([self.message()], [raw])["100"][0]
        self.assertEqual(item["status"], "missing")
        raw = self.row()
        raw["source_table"] = "Msg_" + "0" * 32
        self.assertEqual(self.collect([self.message()], [raw])["100"][0]["status"], "missing")

    def test_duplicate_server_id_without_unique_raw_match_is_missing(self):
        self.source("msg/file/2026-10/synthetic.txt")
        item = self.collect([self.message()], [self.row(), self.row(local_id=5)])["100"][0]
        self.assertEqual(item["status"], "missing")

    def test_other_message_conversation_raises(self):
        first, second = self.message(), self.message(101)
        second["conversation_id"] = "other-conversation"
        with self.assertRaises(ValueError):
            self.collect([first, second], [])

    def test_resource_filename_requires_chat_and_exact_message_discriminators(self):
        self.source("msg/file/2026-10/synthetic.txt")
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE ChatName2Id(user_name TEXT);"
            "CREATE TABLE MessageResourceInfo(message_id INT,chat_id INT,message_svr_id INT,"
            "message_local_id INT,message_create_time INT,message_local_type INT);"
            "CREATE TABLE MessageResourceDetail(message_id INT,packed_info BLOB,size INT,type INT);")
        connection.executemany("INSERT INTO ChatName2Id VALUES(?)", [(self.target,), ("other",)])
        connection.executemany("INSERT INTO MessageResourceInfo VALUES(?,?,?,?,?,?)", [
            (1, 1, 100, 4, self.timestamp, 49), (2, 2, 100, 4, self.timestamp, 49)])
        connection.executemany("INSERT INTO MessageResourceDetail VALUES(?,?,?,?)", [
            (1, b"synthetic.txt", len(self.payload), 0), (2, b"unrelated.txt", len(self.payload), 0)])
        try:
            message = self.message(meta={"kind": "file"})
            with patch.object(media, "_resources", lambda *_: nullcontext(connection)):
                item = self.collect([message], [self.row()])["100"][0]
                self.assertEqual(item["status"], "available")
                self.assertEqual((self.output / item["path"]).read_bytes(), self.payload)
                self.assertEqual(self.collect([message], [self.row(local_id=99)])["100"][0]["status"], "missing")
            details = media._resource_details(connection, self.target, 100, self.row()["row"])
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["packed_info"], b"synthetic.txt")
        finally:
            connection.close()

    def test_strict_video_protobuf_and_base64(self):
        packed = b"\x22\x22\x42\x20" + b"a" * 32
        raw = {"packed_info_data": {"$bytes_base64": base64.b64encode(packed).decode()}}
        self.assertEqual(media._video_base(raw), "a" * 32)
        self.assertIsNone(media._video_base({"packed_info_data": b"a" * 32}))
        self.assertEqual(media._pb(b"\x22\xff"), {})
        self.assertIsNone(media._xml({"message_content": '<!DOCTYPE x [<!ENTITY a "a">]><msg/>'}))

    def test_symlink_source_is_not_followed(self):
        outside = self.root / "unrelated.txt"
        outside.write_bytes(self.payload)
        path = self.account / "msg/file/2026-10/synthetic.txt"
        path.parent.mkdir(parents=True)
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest("creating symlinks is unavailable")
        self.assertIsNone(media._source(self.account, path.relative_to(self.account)))

    def test_count_only_progress_and_caller_keys_remain_unchanged(self):
        self.source("msg/file/2026-10/synthetic.txt")
        events, keys = [], {b"s" * 16: b"k" * 32}
        result = media.collect_video_files([self.message()], [self.row()], self.output,
            account_root=self.account, page_keys=keys, progress_callback=events.append)
        self.assertEqual(result["100"][0]["status"], "available")
        self.assertEqual(keys, {b"s" * 16: b"k" * 32})
        self.assertEqual(events[-1], {"stage": "video_file_progress", "processed": 1, "total": 1})
        self.assertTrue(all(set(event) == {"stage", "processed", "total"} for event in events))

    def test_missing_media_executables_preserves_video_original(self):
        base = "a" * 32
        source = self.source(f"msg/video/2026-10/{base}.mp4", b"synthetic-unplayable-original")
        packed = b"\x22\x22\x42\x20" + base.encode()
        entries = self.collect([self.message(kind=43, meta={})], [self.row(kind=43, packed_info_data=packed)],
            ffmpeg=self.root / "absent-ffmpeg", ffprobe=self.root / "absent-ffprobe")["100"]
        self.assertEqual(entries[0]["status"], "missing")
        self.assertEqual(entries[1]["status"], "available")
        self.assertEqual((self.output / entries[1]["path"]).read_bytes(), source.read_bytes())

    def test_available_ffmpeg_converts_synthetic_mpeg4_and_preserves_original(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("ffmpeg/ffprobe are not on PATH")
        base = "b" * 32
        source = self.account / f"msg/video/2026-10/{base}.mp4"
        source.parent.mkdir(parents=True)
        # Some applications prepend an intentionally stripped ffmpeg to PATH.
        # A co-installed sibling of the discovered ffprobe is another explicit
        # test candidate; no installation path is baked into this test/module.
        sibling = Path(ffprobe).with_name("ffmpeg" + Path(ffprobe).suffix)
        candidates = list(dict.fromkeys([Path(ffmpeg)] + ([sibling] if sibling.is_file() else [])))
        for candidate in candidates:
            generated = subprocess.run([str(candidate), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "color=c=blue:s=64x64:d=0.2", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(source)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if generated.returncode == 0:
                ffmpeg = candidate
                break
        else:
            self.skipTest("available ffmpeg builds cannot generate the synthetic fixture")
        self.assertFalse(media._compatible(media._probe(source, Path(ffprobe))))
        packed = b"\x22\x22\x42\x20" + base.encode()
        entries = self.collect([self.message(kind=43, meta={})], [self.row(kind=43, packed_info_data=packed)],
            ffmpeg=Path(ffmpeg), ffprobe=Path(ffprobe))["100"]
        self.assertEqual(entries[0]["kind"], "video")
        self.assertEqual(entries[0]["status"], "available")
        self.assertTrue(media._compatible(media._probe(self.output / entries[0]["path"], Path(ffprobe))))
        self.assertEqual(entries[1]["kind"], "file")
        self.assertEqual((self.output / entries[1]["path"]).read_bytes(), source.read_bytes())

    def test_encoder_probe_prefers_x264_and_caches_selected_binary_revision(self):
        executable = self.root / "synthetic-ffmpeg.exe"
        executable.write_bytes(b"synthetic executable identity")
        listing = b" V....D h264_mf  H264 via MediaFoundation\n V....D libx264 libx264 H.264\n"
        with patch.object(media.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=listing)) as run:
            options = media._video_encoder_options(executable)
            self.assertEqual(options[1], "libx264")
            self.assertIn("-crf", options)
            self.assertNotIn("-hw_encoding", options)
            self.assertEqual(media._video_encoder_options(executable), options)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command, [str(executable.resolve()), "-nostdin", "-hide_banner", "-encoders"])
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            executable.write_bytes(b"another synthetic executable revision")
            media._video_encoder_options(executable)
            self.assertEqual(run.call_count, 2)

    def test_media_foundation_uses_its_own_software_encoder_controls(self):
        executable = self.root / "synthetic-ffmpeg.exe"
        executable.write_bytes(b"synthetic executable identity")
        with patch.object(media.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=b" V....D h264_mf  H264 via MediaFoundation\n")):
            options = media._video_encoder_options(executable)
        self.assertEqual(options[1], "h264_mf")
        self.assertEqual(options[options.index("-hw_encoding") + 1], "0")
        self.assertIn("-b:v", options)
        self.assertNotIn("-preset", options)
        self.assertNotIn("-crf", options)
        self.assertEqual(options[-2:], ("-pix_fmt", "yuv420p"))

    def test_missing_h264_encoder_never_falls_back_to_mpeg4(self):
        executable = self.root / "synthetic-ffmpeg.exe"
        executable.write_bytes(b"synthetic executable identity")
        original = self.root / "synthetic-original.mp4"
        original.write_bytes(self.payload)
        with patch.object(media, "_probe", return_value=None), patch.object(media.subprocess, "run",
                return_value=SimpleNamespace(returncode=0, stdout=b" V..... mpeg4 MPEG-4\n")) as run:
            playable, reason = media._playable(original, executable, self.root / "synthetic-ffprobe.exe")
        self.assertIsNone(playable)
        self.assertIn("H.264", reason)
        self.assertEqual(original.read_bytes(), self.payload)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(original.with_name(original.stem + "_播放.mp4").exists())

    def test_media_foundation_playback_copy_is_validated_before_publication(self):
        executable = self.root / "synthetic-ffmpeg.exe"
        executable.write_bytes(b"synthetic executable identity")
        original = self.root / "synthetic-original.mp4"
        original.write_bytes(self.payload)
        compatible = {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}],
                      "format": {"format_name": "mov,mp4"}}
        commands = []
        def invoke(command, **kwargs):
            commands.append(command)
            if "-encoders" in command:
                return SimpleNamespace(returncode=0, stdout=b" V....D h264_mf H264 via MediaFoundation\n")
            self.assertEqual(command[command.index("-c:v") + 1], "h264_mf")
            self.assertNotIn("-preset", command)
            self.assertNotIn("-crf", command)
            self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file,pipe")
            Path(command[-1]).write_bytes(b"synthetic validated playback copy")
            return SimpleNamespace(returncode=0)
        with patch.object(media, "_probe", side_effect=[None, compatible]) as probe, \
             patch.object(media.subprocess, "run", side_effect=invoke):
            playable, reason = media._playable(original, executable, self.root / "synthetic-ffprobe.exe")
        self.assertIsNone(reason)
        self.assertEqual(playable.read_bytes(), b"synthetic validated playback copy")
        self.assertEqual(original.read_bytes(), self.payload)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(commands), 2)


if __name__ == "__main__":
    unittest.main()
