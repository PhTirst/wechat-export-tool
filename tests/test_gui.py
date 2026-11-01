"""Native controls exercised with injected synthetic services only."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_export_tool.config import ConfigError, ExportConfig
from wechat_export_tool.gui_jobs import DemoServices


class NativeGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk
        except ImportError:
            raise unittest.SkipTest("Tk desktop is unavailable") from None
        try:
            cls.desktop = tk.Tk()
        except tk.TclError:
            raise unittest.SkipTest("Tk desktop is unavailable") from None
        cls.desktop.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.desktop.destroy()

    def setUp(self):
        import tkinter as tk
        from wechat_export_tool.gui import WeChatExportApp
        self.root = tk.Toplevel(self.desktop)
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory(prefix="synthetic-native-gui-")
        self.fixture = Path(self.directory.name).resolve()
        self.config = ExportConfig(data_root=self.fixture / "not-an-account", account_id="demo-account")
        self.app = WeChatExportApp(self.root, services=DemoServices(), config=self.config,
                                  config_path=self.fixture / "unused-config.json")
        self.pump(lambda: len(self.app._conversations) == 4 and not self.app._operation)

    def tearDown(self):
        if hasattr(self, "app") and not self.app._closed:
            self.app._destroy_window()
        if hasattr(self, "directory"):
            self.directory.cleanup()

    def pump(self, until):
        deadline = time.monotonic() + 10
        while not until() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(until(), "synthetic UI job did not finish")

    def test_search_and_same_name_selection_export_exact_id(self):
        self.app.query_var.set("演示好友")
        self.assertEqual(len(self.app._visible), 2)
        chosen = next(iid for iid, item in self.app._visible.items() if item.conversation_id == "demo-contact-b")
        self.app.tree.selection_set(chosen)
        self.app._update_actions()
        self.assertFalse(self.app.export_button.instate(["disabled"]))
        self.app.output_var.set(str(self.fixture / "exports"))
        self.app._export()
        self.pump(lambda: self.app._last_result is not None and not self.app._operation)
        result = self.app._last_result
        self.assertTrue(result["verified"])
        messages = json.loads((Path(result["output_directory"]) / "messages.json").read_text(encoding="utf-8"))
        self.assertEqual({item["conversation_id"] for item in messages}, {"demo-contact-b"})
        self.assertFalse(self.app.open_page_button.instate(["disabled"]))
        self.assertFalse(self.config.data_root.exists())

    def test_group_filter_clears_contact_selection(self):
        self.app.tree.selection_set("0")
        self.app.kind_var.set("群聊")
        self.assertEqual(len(self.app._visible), 2)
        self.assertTrue(all(item.kind == "group" for item in self.app._visible.values()))
        self.assertIsNone(self.app._selected())
        self.assertTrue(self.app.export_button.instate(["disabled"]))

    def test_demo_disables_capture_and_does_not_save_real_configuration(self):
        self.assertTrue(self.app.capture_button.instate(["disabled"]))
        with patch("wechat_export_tool.gui.save_config") as save:
            self.app._save_settings()
            save.assert_not_called()
        self.assertFalse((self.fixture / "unused-config.json").exists())

    def test_changing_data_root_clears_old_accounts_and_conversations(self):
        self.app.tree.selection_set("0")
        self.app.data_var.set(str(self.fixture / "another-synthetic-location"))
        self.assertEqual(self.app._accounts, [])
        self.assertEqual(self.app._conversations, [])
        self.assertIsNone(self.app._selected())
        self.assertEqual(self.app.account_var.get(), "")
        self.assertTrue(self.app.export_button.instate(["disabled"]))

    def test_close_requests_capture_cancel_and_waits_for_worker_exit(self):
        started, cancelled, allow_exit = threading.Event(), threading.Event(), threading.Event()

        class HoldingCapture(DemoServices):
            demo = False

            def capture(self, _config, *, cancel_event, progress_cb, **kwargs):
                progress_cb({"state": "watching_login", "ready_for_login": True})
                started.set()
                if not cancel_event.wait(5):
                    raise RuntimeError("synthetic cancellation was not requested")
                cancelled.set()
                if not allow_exit.wait(5):
                    raise RuntimeError("synthetic detach was not released")
                return {"cancelled": True, "returncode": 130, "status": "stopped"}

        self.app.services = HoldingCapture()
        self.app._capture()
        self.assertTrue(started.wait(3))
        self.app._close()
        self.assertTrue(cancelled.wait(3))
        self.assertFalse(self.app._closed)
        self.assertTrue(self.app._close_after_capture)
        allow_exit.set()
        self.pump(lambda: self.app._closed)

    def test_config_startup_failure_has_windowed_error_message(self):
        from wechat_export_tool.gui import main
        with patch("wechat_export_tool.gui.load_config", side_effect=ConfigError("synthetic invalid config")), \
             patch("wechat_export_tool.gui.tk.Tk") as make_window, \
             patch("wechat_export_tool.gui.messagebox.showerror") as show_error:
            result = main([])
        self.assertEqual(result, 1)
        show_error.assert_called_once()
        make_window.return_value.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
