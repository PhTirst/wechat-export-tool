"""Synthetic GUI services and worker tests; never open a real WeChat account."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from wechat_export_tool.config import ConfigError, ExportConfig
from wechat_export_tool.errors import ExportError
from wechat_export_tool.gui_jobs import (DemoServices, JobRunner, clean_progress, display_text,
                                         filter_conversations, new_export_directory, safe_error_code)


class GuiServiceTests(unittest.TestCase):
    def test_same_names_remain_distinct_and_filter_by_stable_id(self):
        service = DemoServices()
        config = ExportConfig(data_root=Path("synthetic-demo"), account_id="demo-account")
        contacts = filter_conversations(service.conversations(config), "演示好友", "contact")
        self.assertEqual(len(contacts), 2)
        self.assertNotEqual(contacts[0].conversation_id, contacts[1].conversation_id)
        self.assertEqual(filter_conversations(contacts, "demo-contact-b"), [contacts[1]])
        self.assertEqual(len(filter_conversations(service.conversations(config), kind="group")), 2)
        with self.assertRaises(ConfigError):
            filter_conversations(contacts, kind="invalid")

    def test_error_and_progress_output_never_echo_unknown_values(self):
        self.assertEqual(safe_error_code(RuntimeError("private fixture text")), "operation_failed")
        self.assertEqual(safe_error_code(ConfigError("private fixture text")), "configuration_invalid")
        self.assertEqual(safe_error_code(ExportError("database_key_missing_run_capture")),
                         "database_key_missing_run_capture")
        event = clean_progress({"state": "watching_login", "verified_databases": 2,
            "required_databases": True, "ready_for_login": True, "key": "private fixture text",
            "hint": "private fixture text", "image_key_captured": "private fixture text"})
        self.assertEqual(event, {"state": "watching_login", "verified_databases": 2, "ready_for_login": True})
        for bad in (None, [], "private fixture text", {"state": []}, {"state": "private fixture text"}):
            self.assertEqual(clean_progress(bad), {})

    def test_control_characters_are_not_displayed(self):
        self.assertEqual(display_text(" A\nB\x00\x1bC "), "A B C")

    def test_export_destination_is_new_timestamped_child(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-gui-destination-") as name:
            parent = Path(name).resolve()
            destination = new_export_directory(parent)
            self.assertEqual(destination.parent, parent)
            self.assertRegex(destination.name, r"^wechat_\d{8}_\d{6}_\d{6}$")
            self.assertFalse(destination.exists())

    def test_demo_export_is_scoped_and_contains_verified_local_media(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-gui-demo-") as name:
            root = Path(name).resolve()
            config = ExportConfig(data_root=root / "nonexistent-account-data", account_id="demo-account")
            service = DemoServices()
            conversation = service.conversations(config)[2]
            result = service.export(config, conversation, root / "output")
            self.assertTrue(result["verified"])
            self.assertTrue(result["demo"])
            self.assertFalse(config.data_root.exists())
            output = Path(result["output_directory"])
            messages = json.loads((output / "messages.json").read_text(encoding="utf-8"))
            self.assertEqual({item["conversation_id"] for item in messages}, {conversation.conversation_id})
            self.assertTrue(all(item["is_group_chat"] for item in messages))
            self.assertEqual(len(messages), 5)
            self.assertEqual({media["kind"] for item in messages for media in item["media"]}, {"image", "audio", "file"})
            self.assertTrue((output / "raw_messages.jsonl").is_file())
            self.assertTrue((output / "index.html").is_file())
            with self.assertRaises(ExportError):
                service.export(config, conversation, output)


class GuiWorkerTests(unittest.TestCase):
    def test_capture_can_cancel_without_touching_tk_or_a_process(self):
        runner = JobRunner()
        started = threading.Event()
        seen = []

        def work(cancel, progress):
            seen.append(threading.current_thread().name)
            progress({"state": "watching_login", "key": "private fixture text"})
            started.set()
            self.assertTrue(cancel.wait(3))
            return {"cancelled": True}

        runner.start("capture", work)
        self.assertTrue(started.wait(3))
        with self.assertRaises(RuntimeError):
            runner.start("export", lambda *_: None)
        self.assertTrue(runner.cancel_capture())
        first = runner.events.get(timeout=3)
        final = runner.events.get(timeout=3)
        self.assertEqual(first.kind, "progress")
        self.assertEqual(first.value, {"state": "watching_login"})
        self.assertEqual(final.kind, "done")
        self.assertEqual(final.value, {"cancelled": True})
        self.assertNotEqual(seen, [threading.current_thread().name])

    def test_worker_reports_only_fixed_error_code(self):
        runner = JobRunner()

        def fail(*_):
            raise RuntimeError("private fixture text")

        runner.start("export", fail)
        event = runner.events.get(timeout=3)
        self.assertEqual((event.operation, event.kind, event.value), ("export", "error", "operation_failed"))
        self.assertFalse(runner.cancel_capture())


if __name__ == "__main__":
    unittest.main()
