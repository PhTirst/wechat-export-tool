"""Synthetic regression tests for media-export verification."""
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wechat_export_tool.verify_media_export import verify
from wechat_export_tool import verify_media_export as verifier


_CSP = ("default-src 'none'; img-src file: data:; media-src file:; "
        "style-src 'unsafe-inline'; script-src 'nonce-demo'; connect-src 'none'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'")


class VerifyMediaExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic_verify_export_")
        self.root = Path(self.temp.name) / "export"
        (self.root / "attachments").mkdir(parents=True)
        (self.root / "attachments" / "sample.bin").write_bytes(b"synthetic fixture")
        self.messages = [
            {
                "chat_room": "Synthetic Room",
                "conversation_id": "synthetic-conversation",
                "ts_iso": "2026-10-19T10:20:30+08:00",
                "sender": "Synthetic Alice",
                "is_self": False,
                "content": "synthetic body",
                "msg_type": "text",
                "media": [{
                    "kind": "file", "status": "available",
                    "path": "attachments/sample.bin", "original_path": "",
                    "poster": "", "thumbnail": False,
                }],
            },
            {
                "chat_room": "Synthetic Room",
                "conversation_id": "synthetic-conversation",
                "ts_iso": "2026-10-19T10:21:30+08:00",
                "sender": "Synthetic Bob",
                "is_self": True,
                "content": "another synthetic body",
                "msg_type": "text",
                "media": [{
                    "kind": "image", "status": "missing", "path": "",
                    "original_path": "", "poster": "", "thumbnail": True,
                    "reason": "local_file_not_found",
                }],
            },
        ]
        self._write_export()

    def tearDown(self):
        self.temp.cleanup()

    def _html_media(self, source):
        return {
            "kind": source["kind"], "status": source["status"],
            "path": source.get("path", ""),
            "original_path": source.get("original_path", ""),
            "poster": source.get("poster", ""),
            "thumbnail": bool(source.get("thumbnail")),
            "reason": "本机未找到附件原文件" if source["status"] == "missing" else "",
        }

    def _write_export(self, html_override=None):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "messages.json").write_text(
            json.dumps(self.messages, ensure_ascii=False), encoding="utf-8")
        manifest = {
            "target": "Synthetic Room",
            "messages": len(self.messages),
            "media_references": sum(len(row["media"]) for row in self.messages),
            "counts": {"text:file:available": 1, "text:image:missing": 1},
            "missing_reasons": {"local_file_not_found": 1},
        }
        (self.root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        rows = []
        for message in self.messages:
            timestamp = message["ts_iso"]
            rows.append([
                timestamp[:10], timestamp[:19].replace("T", " "), message["sender"],
                message["is_self"], message["content"], message["msg_type"],
                [self._html_media(item) for item in message["media"]],
            ])
        html_text = (
            '<!doctype html><html><head><meta http-equiv="Content-Security-Policy" '
            f'content="{_CSP}"></head><body>'
            '<h1>Synthetic Room</h1><p>消息 2 条　可用附件 1 项　缺失或不可预览 1 项</p>'
            '<script id="viewer-info" type="application/json">{"title":"Synthetic Room","note":""}</script>'
            '<script id="conversation-data" type="application/json">'
            + json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c")
            + '</script><script nonce="demo">document.body.dataset.ready = "yes";</script>'
            '</body></html>'
        )
        (self.root / "index.html").write_text(html_override or html_text, encoding="utf-8")

    def test_normal_synthetic_export_passes_and_returns_aggregates(self):
        result = verify(self.root)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["totals"]["messages"], 2)
        self.assertEqual(result["totals"]["media_references"], 2)
        self.assertEqual(result["totals"]["html_messages"], 2)

    def test_ffprobe_is_restricted_to_local_protocols_and_supported_containers(self):
        response = SimpleNamespace(returncode=0, stdout=json.dumps({
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        }).encode("utf-8"))
        fixture_path = self.root / "attachments" / "synthetic.mp4"
        with patch.object(verifier.subprocess, "run", return_value=response) as run:
            metadata, error = verifier._probe(fixture_path, "synthetic-ffprobe")
        self.assertEqual(error, "")
        self.assertTrue(metadata["streams"])
        command = run.call_args.args[0]
        self.assertEqual(command[-1], str(fixture_path))
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file,pipe")
        formats = command[command.index("-format_whitelist") + 1].split(",")
        self.assertEqual(set(formats), {"mov", "matroska", "webm", "wav", "mp3", "ogg", "flac", "aac"})
        self.assertNotIn("hls", formats)
        self.assertNotIn("dash", formats)
        self.assertLess(command.index("-protocol_whitelist"), len(command) - 1)
        self.assertLess(command.index("-format_whitelist"), len(command) - 1)

    def test_real_renderer_and_verifier_agree_on_synthetic_export(self):
        from wechat_export_tool.render_media_viewer import render_viewer
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(render_viewer(self.messages, self.root / "index.html", manifest), 2)
        result = verify(self.root, expect_count=2, expect_target="Synthetic Room")
        self.assertTrue(result["verified"], result)

    def test_viewer_title_tampering_is_detected(self):
        path = self.root / "index.html"
        text = path.read_text(encoding="utf-8").replace('"title":"Synthetic Room"', '"title":"Different Room"')
        path.write_text(text, encoding="utf-8")
        self.assertIn("html_viewer_info_mismatch", verify(self.root)["issues"])

    def test_expected_count_and_target_mismatch_are_reported(self):
        result = verify(self.root, expect_count=5, expect_target="Other Room")
        self.assertFalse(result["verified"])
        self.assertIn("expected_message_count_mismatch", result["issues"])
        self.assertIn("target_scope_invalid", result["issues"])

    def test_message_or_html_tampering_is_detected(self):
        html_path = self.root / "index.html"
        html_text = html_path.read_text(encoding="utf-8")
        html_path.write_text(html_text.replace("synthetic body", "tampered body", 1), encoding="utf-8")
        result = verify(self.root)
        self.assertIn("html_message_mismatch", result["issues"])

    def test_external_html_resource_is_rejected(self):
        html_path = self.root / "index.html"
        text = html_path.read_text(encoding="utf-8")
        self._write_export(html_override=text.replace("</body>", '<img src="https://example.invalid/x.png"></body>'))
        result = verify(self.root)
        self.assertIn("html_external_resource", result["issues"])

    def test_csp_that_allows_network_is_rejected(self):
        text = (self.root / "index.html").read_text(encoding="utf-8")
        text = text.replace("connect-src 'none'", "connect-src https:")
        self._write_export(html_override=text)
        self.assertIn("html_csp_invalid", verify(self.root)["issues"])

    def test_csp_duplicate_directive_is_rejected(self):
        text = (self.root / "index.html").read_text(encoding="utf-8")
        text = text.replace("default-src 'none';", "default-src 'none'; default-src https:")
        self._write_export(html_override=text)
        self.assertIn("html_csp_invalid", verify(self.root)["issues"])

    def test_csp_directive_override_is_rejected(self):
        text = (self.root / "index.html").read_text(encoding="utf-8")
        text = text.replace("script-src 'nonce-demo';", "script-src 'nonce-demo'; script-src-attr 'unsafe-inline';")
        self._write_export(html_override=text)
        self.assertIn("html_csp_invalid", verify(self.root)["issues"])

    def test_data_script_source_is_rejected(self):
        text = (self.root / "index.html").read_text(encoding="utf-8")
        text = text.replace("script-src 'nonce-demo'", "script-src 'nonce-demo' data:")
        self._write_export(html_override=text)
        self.assertIn("html_csp_invalid", verify(self.root)["issues"])

    def test_executable_script_with_src_is_rejected(self):
        text = (self.root / "index.html").read_text(encoding="utf-8")
        text = text.replace('<script nonce="demo">', '<script nonce="demo" src="local.js">')
        self._write_export(html_override=text)
        result = verify(self.root)
        self.assertIn("html_csp_invalid", result["issues"])

    def test_event_handler_and_active_element_are_rejected(self):
        text = (self.root / "index.html").read_text(encoding="utf-8")
        text = text.replace("</body>", '<div onclick="run()"></div><iframe></iframe></body>')
        self._write_export(html_override=text)
        self.assertIn("html_active_content", verify(self.root)["issues"])

    def test_unsafe_media_path_is_rejected(self):
        self.messages[0]["media"][0]["path"] = "../outside.bin"
        self._write_export()
        result = verify(self.root)
        self.assertIn("path_unsafe", result["issues"])

    def test_symlink_escape_is_rejected_when_supported(self):
        outside = Path(self.temp.name) / "outside.bin"
        outside.write_bytes(b"synthetic outside target")
        link = self.root / "attachments" / "escape.bin"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable in this environment")
        self.messages[0]["media"][0]["path"] = "attachments/escape.bin"
        self._write_export()
        result = verify(self.root)
        self.assertIn("path_escapes_directory", result["issues"])

    def test_primary_input_symlink_escape_is_rejected_when_supported(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text("[]", encoding="utf-8")
        input_path = self.root / "messages.json"
        input_path.unlink()
        try:
            os.symlink(outside, input_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable in this environment")
        self.assertIn("path_escapes_directory", verify(self.root)["issues"])

    def test_synthetic_wav_is_verified_in_deep_mode(self):
        wav_path = self.root / "attachments" / "synthetic.wav"
        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 80)
        self.messages[0]["media"][0].update(kind="audio", path="attachments/synthetic.wav")
        self._write_export()
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["counts"]["text:audio:available"] = manifest["counts"].pop("text:file:available")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = verify(self.root, deep=True)
        self.assertTrue(result["verified"], result)
        self.assertEqual(result["totals"]["wav_verified"], 1)

    def test_manifest_count_tampering_is_detected(self):
        path = self.root / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["media_references"] = 99
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIn("manifest_media_count_mismatch", verify(self.root)["issues"])


if __name__ == "__main__":
    unittest.main()
