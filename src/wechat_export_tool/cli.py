"""Command line entry point for scoped, local WeChat exports."""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import importlib.metadata
import json
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from .config import ConfigError, ExportConfig, default_config_path, load_config, save_config


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    # argparse errors can echo rejected command-line values. Keep even an
    # accidentally supplied key argument out of application diagnostics.
    def error(self, message: str) -> None:
        raise _UsageError("参数无效，请运行 wechat-export --help。")


def _positive_pid(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("PID must be a positive uint32")
    return int(value)


def _timeout(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 600:
        raise argparse.ArgumentTypeError("timeout must be 1..600 seconds")
    return int(value)


def _add_common(parser: argparse.ArgumentParser) -> None:
    # Suppressed defaults let the same flags work before or after a command
    # without subparser defaults erasing options supplied to the parent.
    parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="用户配置文件")
    parser.add_argument("--data-root", type=Path, default=argparse.SUPPRESS, help="xwechat_files 目录")
    parser.add_argument("--account", dest="account_id", default=argparse.SUPPRESS, help="账号目录稳定 ID")
    for name, help_text in (("cache-dir", "受保护本机缓存目录"), ("capture-exe", "WxMori 可执行文件"),
                            ("ffmpeg", "FFmpeg 路径"), ("ffprobe", "FFprobe 路径"),
                            ("node", "Node.js 路径"), ("silk-module", "Silk 解码模块路径")):
        parser.add_argument("--" + name, type=Path, default=argparse.SUPPRESS, help=help_text)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出机器可读 JSON")


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    _add_common(common)
    parser = _Parser(prog="wechat-export", description="在本机导出指定微信联系人或群聊。", parents=[common])
    try:
        version = importlib.metadata.version("wechat-export-tool")
    except importlib.metadata.PackageNotFoundError:
        version = "development"
    parser.add_argument("--version", action="version", version="%(prog)s " + version)
    commands = parser.add_subparsers(dest="command", parser_class=_Parser)
    commands.add_parser("accounts", parents=[common], help="列出本机微信账号")
    conversations = commands.add_parser("conversations", parents=[common], help="查找联系人或群聊及稳定 ID")
    export = commands.add_parser("export", parents=[common], help="导出一个明确选择的会话")
    for command in (conversations, export):
        command.add_argument("--query", default="", help="备注、昵称或群名关键词")
        command.add_argument("--kind", choices=("all", "contact", "group"), default="all")
    export.add_argument("--conversation-id", help="会话稳定 ID；指定后无需交互")
    export.add_argument("--output", type=Path, help="本次导出的新目录")
    capture = commands.add_parser("capture", parents=[common], help="由本机 WxMori 捕获并保护密钥")
    capture.add_argument("--timeout", type=_timeout, default=180, help="等待秒数，1..600")
    capture.add_argument("--pid", type=_positive_pid, help="限定正常枚举中的 Weixin.exe PID")
    image_selection = capture.add_mutually_exclusive_group()
    image_selection.add_argument("--images-probe", type=Path, help="可选目标图片密文探针 JSON")
    image_selection.add_argument("--conversation-id", help="为指定会话自动准备图片密文探针")
    configuration = commands.add_parser("config", parents=[common], help="查看或保存无密钥的用户配置")
    config_commands = configuration.add_subparsers(dest="config_action", parser_class=_Parser)
    config_commands.add_parser("show", parents=[common], help="显示当前有效配置")
    setter = config_commands.add_parser("set", parents=[common], help="保存命令行提供的配置选项")
    setter.add_argument("--clear-account", action="store_true", help="清除已保存账号，下次重新选择")
    return parser


def _clean_display(value: object) -> str:
    # Contact names are untrusted display data, including terminal controls.
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value or "")).split())


_SENSITIVE_FIELDS = frozenset(("key", "aes", "aes_key", "image_key", "master_key", "database_keys",
                               "database_key", "enc_key", "page_key", "encryption_key", "decryption_key",
                               "password", "token", "secret", "protected", "key_material", "api_key",
                               "authorization", "access_token", "by_salt", "salt_keys"))


def _safe_output(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return {str(name): "[redacted]" if str(name).lower() in _SENSITIVE_FIELDS else _safe_output(item)
                for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_output(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return "[unsupported value]"


def _print_json(value, output: TextIO) -> None:
    print(json.dumps(_safe_output(value), ensure_ascii=False, indent=2), file=output)


def _interactive(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _choose(items, *, label, prompt: str, input_stream: TextIO, error_stream: TextIO):
    for index, item in enumerate(items, 1):
        print(f"{index}. {label(item)}", file=error_stream)
    for _ in range(3):
        print(prompt + "（输入编号，q 取消）：", end="", flush=True, file=error_stream)
        answer = input_stream.readline().strip()
        if not answer or answer.lower() == "q":
            raise KeyboardInterrupt
        if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        print("请选择列表中的编号。", file=error_stream)
    raise _UsageError("连续输入无效，请使用稳定 ID 指定目标。")


def _account_label(account) -> str:
    return f"{_clean_display(account.display_name) or '微信账号'} [{_clean_display(account.account_id)}]"


def _conversation_label(conversation) -> str:
    kind = "群聊" if conversation.kind == "group" else "联系人"
    count = f" · {conversation.message_count} 条" if conversation.message_count is not None else ""
    return f"{_clean_display(conversation.display_name)} · {kind}{count} [{_clean_display(conversation.conversation_id)}]"


def _select_account(config: ExportConfig, engine, *, input_stream: TextIO, error_stream: TextIO,
                    json_mode: bool) -> ExportConfig:
    if config.account_id is not None:
        return config
    accounts = engine.discover_accounts(config.data_root)
    if not accounts:
        raise _UsageError("未发现微信账号，请检查 --data-root。")
    if len(accounts) == 1:
        selected = accounts[0]
    elif not json_mode and _interactive(input_stream):
        selected = _choose(accounts, label=_account_label, prompt="选择微信账号",
                           input_stream=input_stream, error_stream=error_stream)
    else:
        raise _UsageError("存在多个微信账号；请先运行 accounts，再使用 --account 指定账号。")
    return dataclasses.replace(config, account_id=selected.account_id)


def _effective_config(arguments: argparse.Namespace) -> ExportConfig:
    config = load_config(getattr(arguments, "config", None))
    options = {field.name: getattr(arguments, field.name) for field in dataclasses.fields(config)
               if hasattr(arguments, field.name)}
    if getattr(arguments, "clear_account", False):
        options["account_id"] = None
    return dataclasses.replace(config, **options) if options else config


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    input_stream, output, errors = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    parser = build_parser()
    try:
        with redirect_stdout(output), redirect_stderr(errors):
            arguments = parser.parse_args(argv)
        if not arguments.command:
            parser.print_help(file=output)
            return 0
        config = _effective_config(arguments)
        json_mode = getattr(arguments, "json", False)
        if arguments.command == "config":
            if arguments.config_action == "set":
                path = save_config(config, getattr(arguments, "config", None))
                _print_json({"saved": True, "config": path}, output)
            elif arguments.config_action in ("show", None):
                _print_json(config.to_dict(), output)
            return 0
        engine = importlib.import_module("wechat_export_tool.engine")
        if arguments.command == "accounts":
            accounts = engine.discover_accounts(config.data_root)
            if json_mode:
                _print_json(accounts, output)
            else:
                for account in accounts:
                    print(_account_label(account), file=output)
                if not accounts:
                    print("未发现微信账号。", file=output)
            return 0
        config = _select_account(config, engine, input_stream=input_stream, error_stream=errors,
                                 json_mode=json_mode)
        if arguments.command == "capture":
            backend = importlib.import_module("wechat_export_tool.backend")
            probe = arguments.images_probe
            if arguments.conversation_id:
                probe = backend.prepare_image_probe(config, arguments.conversation_id)
            result = backend.capture_keys(config, timeout=arguments.timeout, pid=arguments.pid,
                                          images_probe=probe)
            _print_json(result, output)
            return 1 if isinstance(result, dict) and result.get("returncode", 0) != 0 else 0
        conversations = None
        if arguments.command == "conversations" or not arguments.conversation_id:
            conversations = engine.list_conversations(config, query=arguments.query, kind=arguments.kind)
        if arguments.command == "conversations":
            if json_mode:
                _print_json(conversations, output)
            else:
                for conversation in conversations:
                    print(_conversation_label(conversation), file=output)
                if not conversations:
                    print("未找到匹配的联系人或群聊。", file=output)
            return 0
        conversation_id = arguments.conversation_id
        if not conversation_id:
            if not conversations:
                raise _UsageError("未找到匹配会话；请调整 --query 或 --kind。")
            if len(conversations) == 1:
                selected = conversations[0]
            elif not json_mode and _interactive(input_stream):
                selected = _choose(conversations, label=_conversation_label, prompt="选择要导出的会话",
                                   input_stream=input_stream, error_stream=errors)
            else:
                raise _UsageError("匹配多个会话；请先运行 conversations，再使用 --conversation-id 精确导出。")
            conversation_id = selected.conversation_id
        destination = arguments.output
        if destination is None:
            timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_%f")
            destination = Path.cwd() / "exports" / ("wechat_" + timestamp)
        result = engine.export_conversation(config, conversation_id, destination)
        _print_json(result, output)
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except (ConfigError, _UsageError) as exc:
        print(str(exc), file=errors)
        return 2
    except KeyboardInterrupt:
        print("已取消。", file=errors)
        return 130
    except Exception as exc:
        # Database parsers and native tools may put secrets in their exception
        # messages. Only the exception class is allowed into this diagnostic.
        from .errors import ExportError
        detail = exc.code if isinstance(exc, ExportError) else type(exc).__name__
        print(f"操作失败（{detail}）。请检查配置和本机密钥捕获状态。", file=errors)
        return 1
