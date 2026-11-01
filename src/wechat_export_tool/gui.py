"""Small native desktop UI sharing the same scoped export engine as the CLI."""
from __future__ import annotations

import argparse
import dataclasses
import os
import queue
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import ConfigError, ExportConfig, load_config, save_config
from .gui_jobs import (DemoServices, JobEvent, JobRunner, LocalServices, display_text,
                       filter_conversations, new_export_directory)
from .models import Account, Conversation


_KINDS = {"全部": "all", "联系人": "contact", "群聊": "group"}
_CAPTURE_STATES = {
    "waiting_for_wechat": "等待本机微信打开。",
    "waiting_for_database": "等待所选账号的数据库加载。",
    "waiting_for_databases": "等待所选账号的数据库加载。",
    "arming_login_capture": "正在准备登录监听，请稍候。",
    "watching_login": "监听已就绪。请在微信中退出登录后重新登录；随后查看所需的历史或播放语音。",
    "watching_images": "图片监听已就绪，请在所选会话中打开本地图片。",
    "scanning": "正在检查本机数据库密钥。",
    "settling": "已取得密钥，等待数据库列表稳定。",
    "completed": "本次监听已完成。",
    "partial": "已取得部分密钥，部分历史或附件可能仍无法导出。",
    "timeout": "监听时间已到；已取得的密钥仍保留在本机受保护缓存中。",
    "stopped": "监听已停止。",
    "stopping": "正在停止监听并等待取钥程序安全退出，请勿强制结束进程……",
    "access_denied": "无法访问目标微信进程，请检查是否与当前用户和权限一致。",
    "account_mismatch": "当前加载的微信账号与所选账号不一致。",
    "unsupported": "当前微信版本的取钥位置暂不受支持。",
    "failed": "本次取钥未完成，请检查微信版本和所选账号。",
}
_ERRORS = {
    "database_key_missing_run_capture": "数据库密钥尚未就绪。请先点击“获取本机密钥”，完成后重新读取会话。",
    "capture_executable_missing_run_build_capture": "未找到取钥程序。请使用完整的 Windows 发布包，或在配置中指定 capture_exe。",
    "account_database_directory_missing": "所选账号中未找到数据库目录，请检查微信数据目录。",
    "no_local_v2_image_for_selected_conversation": "所选会话没有可用于图片取钥的本地样本。可先取消图片选项，获取正文数据库密钥。",
    "conversation_has_no_local_messages": "所选会话在本机没有可导出的消息。",
    "output_must_be_outside_account": "导出目录不能位于微信账号的数据目录中。",
    "output_directory_must_be_empty": "导出目标已存在且不为空，请选择其他位置。",
    "export_validation_failed": "导出文件未通过完整性检查，不能作为成功备份。请保留现场并检查兼容性。",
    "configuration_invalid": "配置无效，请检查数据目录和账号选择。",
}


class WeChatExportApp:
    """All widgets and event dispatch stay on the thread that owns Tk."""

    def __init__(self, root: tk.Tk, *, services=None, config: ExportConfig | None = None,
                 config_path: Path | None = None, interactive_errors: bool = True):
        self.root = root
        self.services = services or LocalServices()
        self.config = config or ExportConfig()
        self.config_path = config_path
        self.interactive_errors = interactive_errors
        self.runner = JobRunner()
        self._accounts: list[Account] = []
        self._conversations: list[Conversation] = []
        self._visible: dict[str, Conversation] = {}
        self._operation = ""
        self._close_after_capture = False
        self._closed = False
        self._last_result: dict | None = None
        self._export_target: Conversation | None = None
        self.data_var = tk.StringVar(root, value=str(self.config.data_root))
        self.account_var = tk.StringVar(root)
        self.query_var = tk.StringVar(root)
        self.kind_var = tk.StringVar(root, value="全部")
        self.output_var = tk.StringVar(root, value=str(Path.home() / "Documents" / "WeChatExports"))
        self.timeout_var = tk.StringVar(root, value="300")
        self.images_var = tk.BooleanVar(root, value=False)
        self.status_var = tk.StringVar(root, value="正在查找本地账号……")
        self.selection_var = tk.StringVar(root, value="请选择一个联系人或群聊。")
        self.result_var = tk.StringVar(root)
        self._interactive: list = []
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.query_var.trace_add("write", lambda *_: self._filter())
        self.kind_var.trace_add("write", lambda *_: self._filter())
        self.data_var.trace_add("write", lambda *_: self._data_changed())
        self._poll_id = root.after(80, self._poll)
        root.after_idle(self._refresh_accounts)

    def _build(self):
        demo = bool(getattr(self.services, "demo", False))
        self.root.title("微信聊天记录导出" + (" · 合成演示" if demo else ""))
        self.root.geometry("1000x780")
        self.root.minsize(860, 700)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Muted.TLabel", foreground="#53616b")
        style.configure("Treeview", rowheight=29)
        body = ttk.Frame(self.root, padding=(20, 16))
        body.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1); self.root.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1); body.rowconfigure(5, weight=1)
        ttk.Label(body, text="微信聊天记录导出", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        subtitle = "仅导出本机保存的消息与附件 · JSON + 离线网页"
        if demo:
            subtitle = "合成演示模式 · 不读取真实聊天或密钥，不访问微信进程"
        ttk.Label(body, text=subtitle, style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 16))

        ttk.Label(body, text="微信数据目录").grid(row=2, column=0, sticky="w", padx=(0, 12))
        data = ttk.Entry(body, textvariable=self.data_var)
        data.grid(row=2, column=1, sticky="ew", pady=4)
        data.bind("<Return>", lambda _: self._refresh_accounts())
        browse_data = ttk.Button(body, text="浏览…", command=self._browse_data)
        browse_data.grid(row=2, column=2, padx=(8, 0))
        account_frame = ttk.Frame(body)
        account_frame.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(4, 10))
        account_frame.columnconfigure(0, weight=1)
        ttk.Label(body, text="本地账号").grid(row=3, column=0, sticky="w")
        self.account_box = ttk.Combobox(account_frame, textvariable=self.account_var, state="readonly")
        self.account_box.grid(row=0, column=0, sticky="ew")
        self.account_box.bind("<<ComboboxSelected>>", lambda _: self._account_changed())
        refresh = ttk.Button(account_frame, text="刷新账号", command=self._refresh_accounts)
        refresh.grid(row=0, column=1, padx=(8, 0))
        save = ttk.Button(account_frame, text="保存设置", command=self._save_settings)
        save.grid(row=0, column=2, padx=(8, 0))

        capture = ttk.LabelFrame(body, text="首次使用或缺少密钥时", padding=(10, 8))
        capture.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        capture.columnconfigure(5, weight=1)
        self.capture_button = ttk.Button(capture, text="获取本机密钥", command=self._capture)
        self.capture_button.grid(row=0, column=0, sticky="w")
        ttk.Label(capture, text="等待").grid(row=0, column=1, padx=(12, 4))
        self.timeout_box = ttk.Combobox(capture, textvariable=self.timeout_var, values=("180", "300", "600"), state="readonly", width=5)
        self.timeout_box.grid(row=0, column=2)
        ttk.Label(capture, text="秒").grid(row=0, column=3, padx=(4, 12))
        self.images_check = ttk.Checkbutton(capture, text="同时尝试所选会话的图片密钥", variable=self.images_var)
        self.images_check.grid(row=0, column=4, sticky="w")
        self.cancel_button = ttk.Button(capture, text="停止监听", command=self._cancel_capture, state="disabled")
        self.cancel_button.grid(row=0, column=6, padx=(8, 0))
        ttk.Label(capture, text="这是独立的本机操作。监听就绪后按状态提示操作微信；程序不会自动重启微信。",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=7, sticky="w", pady=(7, 0))

        conversations = ttk.LabelFrame(body, text="选择一个联系人或群聊", padding=10)
        conversations.grid(row=5, column=0, columnspan=3, sticky="nsew")
        conversations.columnconfigure(0, weight=1); conversations.rowconfigure(1, weight=1)
        filters = ttk.Frame(conversations)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text="搜索").grid(row=0, column=0, padx=(0, 8))
        self.query_entry = ttk.Entry(filters, textvariable=self.query_var)
        self.query_entry.grid(row=0, column=1, sticky="ew")
        self.kind_box = ttk.Combobox(filters, textvariable=self.kind_var, values=tuple(_KINDS), state="readonly", width=8)
        self.kind_box.grid(row=0, column=2, padx=8)
        self.load_button = ttk.Button(filters, text="读取联系人 / 群聊", command=self._load_conversations)
        self.load_button.grid(row=0, column=3)
        self.tree = ttk.Treeview(conversations, columns=("name", "kind", "id"), show="headings", selectmode="browse")
        for name, label, width in (("name", "备注 / 名称", 300), ("kind", "类型", 80), ("id", "会话标识（同名时用于区分）", 340)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=65, stretch=name != "kind")
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(conversations, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._update_actions())
        ttk.Label(conversations, textvariable=self.selection_var, style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))

        ttk.Label(body, text="导出保存到").grid(row=6, column=0, sticky="w", pady=(13, 0))
        self.output_entry = ttk.Entry(body, textvariable=self.output_var)
        self.output_entry.grid(row=6, column=1, sticky="ew", pady=(13, 0))
        browse_output = ttk.Button(body, text="浏览…", command=self._browse_output)
        browse_output.grid(row=6, column=2, padx=(8, 0), pady=(13, 0))
        ttk.Label(body, text="每次新建独立导出目录，保留 JSON、原始行和含附件的离线网页。",
                  style="Muted.TLabel").grid(row=7, column=1, columnspan=2, sticky="w", pady=(4, 8))
        actions = ttk.Frame(body)
        actions.grid(row=8, column=0, columnspan=3, sticky="ew")
        self.export_button = ttk.Button(actions, text="导出所选会话", command=self._export, state="disabled")
        self.export_button.pack(side="left")
        self.open_page_button = ttk.Button(actions, text="打开离线网页", command=self._open_page, state="disabled")
        self.open_page_button.pack(side="left", padx=(10, 0))
        self.open_folder_button = ttk.Button(actions, text="打开导出目录", command=self._open_folder, state="disabled")
        self.open_folder_button.pack(side="left", padx=(8, 0))
        self.progress_bar = ttk.Progressbar(actions, mode="indeterminate", length=125)
        self.progress_bar.pack(side="right")
        ttk.Label(body, textvariable=self.result_var, wraplength=880).grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Separator(body).grid(row=10, column=0, columnspan=3, sticky="ew", pady=(10, 7))
        ttk.Label(body, textvariable=self.status_var, wraplength=880, style="Muted.TLabel").grid(row=11, column=0, columnspan=3, sticky="w")
        self._interactive = [data, browse_data, self.account_box, refresh, save, self.capture_button,
            self.timeout_box, self.images_check, self.query_entry, self.kind_box, self.load_button,
            self.output_entry, browse_output]
        self._update_actions()

    def _current_config(self, *, account_required: bool = True) -> ExportConfig:
        value = self.data_var.get().strip()
        if not value:
            raise ConfigError("请选择微信数据目录。")
        selected = self.account_box.current()
        account = self._accounts[selected].account_id if 0 <= selected < len(self._accounts) else None
        if account_required and account is None:
            raise ConfigError("请先选择本地微信账号。")
        return dataclasses.replace(self.config, data_root=Path(value), account_id=account)

    def _selected(self) -> Conversation | None:
        selected = self.tree.selection()
        return self._visible.get(selected[0]) if len(selected) == 1 else None

    def _show_error(self, code: str):
        text = _ERRORS.get(code, f"操作未完成（{code}）。请检查配置、密钥状态及客户端兼容性。")
        self.status_var.set(text)
        if self.interactive_errors:
            messagebox.showerror("未能完成操作", text, parent=self.root)

    def _start(self, operation: str, work, status: str):
        if self._operation:
            return
        self._operation = operation
        self.status_var.set(status)
        for control in self._interactive:
            control.state(["disabled"])
        self.progress_bar.start(12)
        self._update_actions()
        self.runner.start(operation, work)

    def _idle(self):
        self._operation = ""
        self.progress_bar.stop()
        for control in self._interactive:
            control.state(["!disabled"])
        self.account_box.state(["readonly"]); self.kind_box.state(["readonly"]); self.timeout_box.state(["readonly"])
        self._update_actions()

    def _poll(self):
        if self._closed:
            return
        self._poll_id = None
        try:
            while True:
                event = self.runner.events.get_nowait()
                self._dispatch(event)
        except queue.Empty:
            pass
        if not self._closed:
            self._poll_id = self.root.after(80, self._poll)

    def _destroy_window(self):
        self._closed = True
        if self._poll_id is not None:
            self.root.after_cancel(self._poll_id)
            self._poll_id = None
        self.root.destroy()

    def _dispatch(self, event: JobEvent):
        if event.kind == "progress":
            value = event.value
            text = _CAPTURE_STATES.get(value.get("state"), "取钥监听正在运行。")
            if value.get("ready_for_login") and value.get("state") in {"watching_login", "arming_login_capture"}:
                text = "监听已就绪。请在微信中退出登录后重新登录，再查看所需历史或播放语音。"
            if "verified_databases" in value:
                text += f" 已验证数据库 {value['verified_databases']} / {value.get('required_databases', '?')}。"
            self.status_var.set(text)
            return
        self._idle()
        if self._close_after_capture and event.operation == "capture":
            self._destroy_window()
            return
        if event.kind == "error":
            self._show_error(event.value)
            return
        value = event.value
        if event.operation == "accounts":
            self._accounts = value
            self.account_box.configure(values=[f"{display_text(item.display_name) or '微信账号'} [{display_text(item.account_id)}]" for item in value])
            if value:
                remembered = next((i for i, item in enumerate(value) if item.account_id == self.config.account_id), 0)
                self.account_box.current(remembered)
                self.status_var.set(f"找到 {len(value)} 个本地账号。选择账号后读取联系人 / 群聊；缺少密钥时先获取本机密钥。")
                if getattr(self.services, "demo", False):
                    self.root.after_idle(self._load_conversations)
            else:
                self.account_var.set("")
                self.status_var.set("未发现本地账号，请选择包含账号文件夹的 xwechat_files 数据目录。")
        elif event.operation == "conversations":
            self._conversations = value
            self._filter()
            self.status_var.set(f"已读取 {len(value)} 个联系人 / 群聊。搜索并选择一个会话后导出。")
        elif event.operation == "capture":
            if value.get("cancelled"):
                self.status_var.set("已停止监听，取钥程序已退出。可以读取会话或稍后继续。")
            else:
                self.status_var.set(_CAPTURE_STATES.get(value.get("status"), "本次取钥已结束。") + " 现在可以尝试读取联系人 / 群聊。")
        elif event.operation == "export":
            self._last_result = value
            missing = sum(value.get("missing_reasons", {}).values())
            target = display_text(self._export_target.display_name) if self._export_target else "所选会话"
            self.result_var.set(f"“{target}”导出完成：{value.get('messages', 0)} 条消息，{missing} 个附件引用未导出。文件位于：{value.get('output_directory', '')}")
            self.status_var.set("消息与离线文件已通过校验。附件是否齐全以网页中的缺失说明为准；请把整个导出目录作为私人备份保存。")
        self._update_actions()

    def _clear_conversations(self):
        self._conversations = []
        self._filter()

    def _refresh_accounts(self):
        if self._operation:
            return
        try:
            config = self._current_config(account_required=False)
        except ConfigError:
            self._show_error("configuration_invalid"); return
        self._clear_conversations()
        self._start("accounts", lambda _cancel, _progress: self.services.accounts(config), "正在查找本地账号……")

    def _account_changed(self):
        self._clear_conversations()
        self.status_var.set("账号已切换，请重新读取联系人 / 群聊。")

    def _data_changed(self):
        if self._operation:
            return
        self._accounts = []
        self.account_box.configure(values=())
        self.account_var.set("")
        self._clear_conversations()
        self.status_var.set("微信数据目录已修改，请刷新账号并重新读取联系人 / 群聊。")

    def _load_conversations(self):
        if self._operation:
            return
        try:
            config = self._current_config()
        except ConfigError:
            self._show_error("configuration_invalid"); return
        self._clear_conversations()
        self._start("conversations", lambda _cancel, _progress: self.services.conversations(config), "正在校验数据库并读取联系人 / 群聊……")

    def _filter(self):
        previous = self._selected()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._visible.clear()
        items = filter_conversations(self._conversations, self.query_var.get(), _KINDS[self.kind_var.get()])
        for index, conversation in enumerate(items):
            iid = str(index)
            self._visible[iid] = conversation
            self.tree.insert("", "end", iid=iid, values=(display_text(conversation.display_name),
                "群聊" if conversation.kind == "group" else "联系人", display_text(conversation.conversation_id)))
            if previous and conversation.conversation_id == previous.conversation_id:
                self.tree.selection_set(iid)
        self._update_actions()

    def _update_actions(self):
        selected = self._selected()
        idle = not self._operation
        if selected:
            self.selection_var.set(f"已选择：{display_text(selected.display_name)}  ·  {display_text(selected.conversation_id)}")
        else:
            self.selection_var.set(f"显示 {len(self._visible)} 项；请选择一个联系人或群聊。")
        self.export_button.state(["!disabled"] if idle and selected else ["disabled"])
        self.cancel_button.state(["!disabled"] if self._operation == "capture" else ["disabled"])
        available = idle and self._last_result is not None and self._last_result.get("verified") is True
        self.open_page_button.state(["!disabled"] if available else ["disabled"])
        self.open_folder_button.state(["!disabled"] if available else ["disabled"])
        if idle:
            has_account = 0 <= self.account_box.current() < len(self._accounts)
            self.load_button.state(["!disabled"] if has_account else ["disabled"])
            self.capture_button.state(["!disabled"] if has_account and not getattr(self.services, "demo", False) else ["disabled"])

    def _capture(self):
        try:
            config = self._current_config()
            timeout = int(self.timeout_var.get())
        except (ConfigError, ValueError):
            self._show_error("configuration_invalid"); return
        selected = self._selected()
        if self.images_var.get() and selected is None:
            messagebox.showinfo("需要选择会话", "请先选择一个联系人或群聊，或取消图片密钥选项。", parent=self.root)
            return
        identifier = selected.conversation_id if self.images_var.get() else None
        self._start("capture", lambda cancel, progress: self.services.capture(config, timeout=timeout,
            conversation_id=identifier, cancel_event=cancel, progress_cb=progress),
            "正在启动本机取钥监听。请等待“监听已就绪”后再操作微信。")

    def _cancel_capture(self):
        if self.runner.cancel_capture():
            self.cancel_button.state(["disabled"])
            self.status_var.set("正在停止监听并等待取钥程序安全退出，请勿强制结束进程……")

    def _export(self):
        selected = self._selected()
        if selected is None:
            return
        try:
            config = self._current_config()
            parent = self.output_var.get().strip()
            if not parent:
                raise ConfigError("导出目录为空。")
            destination = new_export_directory(Path(parent))
        except ConfigError:
            self._show_error("configuration_invalid"); return
        self._last_result = None
        self._export_target = selected
        self.result_var.set("")
        self._start("export", lambda _cancel, _progress: self.services.export(config, selected, destination),
            "正在导出所选会话并处理附件。大量历史可能需要较长时间，请等待完成。")

    def _browse_data(self):
        value = filedialog.askdirectory(title="选择 xwechat_files 数据目录", initialdir=self.data_var.get(), parent=self.root)
        if value:
            self.data_var.set(value)
            self._refresh_accounts()

    def _browse_output(self):
        value = filedialog.askdirectory(title="选择导出保存位置", initialdir=self.output_var.get(), parent=self.root)
        if value:
            self.output_var.set(value)

    def _save_settings(self):
        if getattr(self.services, "demo", False):
            self.status_var.set("合成演示模式不会保存或覆盖真实用户配置。")
            return
        try:
            config = self._current_config(account_required=False)
            from .runtime import without_bundled_defaults
            save_config(without_bundled_defaults(config), self.config_path)
            self.config = config
        except ConfigError:
            self._show_error("configuration_invalid"); return
        self.status_var.set("已保存数据目录、账号及工具配置。数据库密钥不会写入配置文件。")

    def _result_path(self) -> Path | None:
        if self._last_result and self._last_result.get("verified") is True:
            return Path(self._last_result["output_directory"])
        return None

    def _open_page(self):
        path = self._result_path()
        if path and (path / "index.html").is_file():
            webbrowser.open((path / "index.html").resolve().as_uri())

    def _open_folder(self):
        path = self._result_path()
        if path and path.is_dir():
            if os.name == "nt":
                os.startfile(str(path))
            else:
                webbrowser.open(path.resolve().as_uri())

    def _close(self):
        if self._operation == "capture":
            self._close_after_capture = True
            self._cancel_capture()
        elif self._operation:
            messagebox.showinfo("正在处理", "请等待当前操作完成后关闭窗口，以保留完整导出。", parent=self.root)
        else:
            self._destroy_window()


def main(argv: list[str] | None = None, *, services=None, config: ExportConfig | None = None) -> int:
    parser = argparse.ArgumentParser(description="微信聊天记录导出图形界面")
    parser.add_argument("--config", type=Path, help="用户配置文件")
    parser.add_argument("--output", type=Path, help="初始导出保存位置；每次导出新建子目录")
    parser.add_argument("--demo", action="store_true", help="仅使用合成数据预览界面，不访问真实微信")
    parser.add_argument("--smoke-test", action="store_true", help="配合 --demo 自动验证选择与导出后退出")
    parser.add_argument("--smoke-report", type=Path, help="将合成冒烟测试的结果写入 JSON")
    args = parser.parse_args(argv)
    if args.smoke_test and not args.demo:
        parser.error("--smoke-test 仅支持 --demo。")
    if args.smoke_report and not args.smoke_test:
        parser.error("--smoke-report 仅支持 --smoke-test。")
    smoke_directory = None
    smoke_result = {"success": False}
    root = None
    try:
        if args.demo:
            services = DemoServices()
            config = config or ExportConfig(data_root=Path.cwd() / "demo-data", account_id="demo-account")
        else:
            config = config or load_config(args.config)
        from .runtime import apply_bundled_defaults
        config = apply_bundled_defaults(config)
        root = tk.Tk()
        app = WeChatExportApp(root, services=services, config=config, config_path=args.config,
                              interactive_errors=not args.smoke_test)
        if args.output is not None:
            app.output_var.set(str(args.output.expanduser().absolute()))
        if args.smoke_test:
            import tempfile
            import time
            if args.output is None:
                smoke_directory = tempfile.TemporaryDirectory(prefix="synthetic-gui-smoke-")
                app.output_var.set(smoke_directory.name)
            deadline = time.monotonic() + 45
            phase = {"export_started": False}

            def smoke():
                if app._closed:
                    return
                if time.monotonic() > deadline:
                    app._destroy_window(); return
                if not app._operation and app._conversations and not phase["export_started"]:
                    # Select the second same-name contact by its stable ID.
                    app.query_var.set("演示好友")
                    chosen = next((iid for iid, item in app._visible.items()
                                   if item.conversation_id == "demo-contact-b"), None)
                    if chosen is None:
                        app._destroy_window(); return
                    app.tree.selection_set(chosen)
                    app._update_actions()
                    phase["export_started"] = True
                    app._export()
                elif phase["export_started"] and not app._operation:
                    result = app._last_result or {}
                    path = app._result_path()
                    smoke_result["success"] = bool(result.get("verified") and result.get("demo")
                        and path and (path / "index.html").is_file()
                        and result.get("messages") == 5)
                    if smoke_result["success"]:
                        smoke_result.update({"output_directory": str(path), "messages": 5,
                                             "conversation_id": "demo-contact-b", "verified": True})
                    app._destroy_window(); return
                root.after(80, smoke)

            root.after(100, smoke)
        root.mainloop()
    except (tk.TclError, ConfigError):
        text = "无法启动图形界面，请检查本机桌面环境与配置。"
        if sys.stderr is not None:
            print(text, file=sys.stderr)
        if not args.smoke_test:
            try:
                if root is None:
                    root = tk.Tk()
                root.withdraw()
                messagebox.showerror("无法启动图形界面", text, parent=root)
            except tk.TclError:
                pass
            finally:
                if root is not None:
                    try:
                        root.destroy()
                    except tk.TclError:
                        pass
        return 1
    finally:
        if args.smoke_report:
            import json
            from .security import private_text_writer
            with private_text_writer(args.smoke_report.expanduser().absolute()) as handle:
                json.dump(smoke_result, handle, ensure_ascii=False, indent=2)
        if smoke_directory is not None:
            smoke_directory.cleanup()
    if args.smoke_test:
        return 0 if smoke_result["success"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
