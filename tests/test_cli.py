import io
import json
import sys
import types
from dataclasses import replace

import pytest

from wechat_export_tool.cli import main
from wechat_export_tool.config import ExportConfig, load_config, save_config
from wechat_export_tool.models import Account, Conversation


class TTY(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def cli_environment(tmp_path, monkeypatch):
    config = ExportConfig(data_root=tmp_path / "data", account_id="synthetic_account",
                          cache_dir=tmp_path / "cache")
    config_path = save_config(config, tmp_path / "config.json")
    calls = []
    engine = types.ModuleType("wechat_export_tool.engine")
    engine.discover_accounts = lambda root: [Account("synthetic_account", root / "synthetic_account", "Synthetic")]
    engine.list_conversations = lambda settings, query="", kind="all": []

    def export(settings, conversation_id, output):
        calls.append((settings, conversation_id, output))
        return {"raw_rows": 2, "output_directory": output}

    engine.export_conversation = export
    backend = types.ModuleType("wechat_export_tool.backend")
    backend.capture_keys = lambda settings, **kwargs: {"completed": True}
    runtime = types.ModuleType("wechat_export_tool.runtime")
    runtime.apply_bundled_defaults = lambda settings: settings
    runtime.without_bundled_defaults = lambda settings: settings
    runtime.runtime_report = lambda settings=None: {"schema_version": 1, "version": "synthetic-version",
        "frozen": False, "tools": {"capture": {"available": False}}, "capabilities": {}}
    monkeypatch.setitem(sys.modules, "wechat_export_tool.engine", engine)
    monkeypatch.setitem(sys.modules, "wechat_export_tool.backend", backend)
    monkeypatch.setitem(sys.modules, "wechat_export_tool.runtime", runtime)

    def invoke(arguments, input_stream=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(["--config", str(config_path), *arguments], stdin=input_stream or io.StringIO(),
                    stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    return types.SimpleNamespace(config=config, path=config_path, engine=engine, backend=backend,
                                 calls=calls, invoke=invoke, tmp=tmp_path)


def test_help_does_not_import_database_engine_or_read_configuration(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("help must not load configuration")

    monkeypatch.setattr("wechat_export_tool.cli.load_config", forbidden)
    output = io.StringIO()
    assert main(["--help"], stdout=output, stderr=io.StringIO()) == 0
    assert "--conversation-id" not in output.getvalue()  # Available under export --help.
    assert "accounts" in output.getvalue()


def test_export_explicit_id_is_noninteractive_and_does_not_search_other_chats(cli_environment):
    env = cli_environment

    def forbidden(*args, **kwargs):
        raise AssertionError("explicit selection must not enumerate")

    env.engine.list_conversations = forbidden
    env.engine.discover_accounts = forbidden
    destination = env.tmp / "result"
    code, output, error = env.invoke(["export", "--conversation-id", "stable_direct_id", "--output", str(destination)])
    assert code == 0 and not error
    assert env.calls[0][1:] == ("stable_direct_id", destination)
    assert json.loads(output)["raw_rows"] == 2


def test_conversation_query_preserves_group_filter_and_stable_ids(cli_environment):
    env = cli_environment
    queries = []

    def listing(config, query="", kind="all"):
        queries.append((query, kind))
        return [Conversation("group_stable_id", "Same name", "group", 12)]

    env.engine.list_conversations = listing
    code, output, _ = env.invoke(["conversations", "--query", "Same", "--kind", "group", "--json"])
    assert code == 0
    assert queries == [("Same", "group")]
    assert json.loads(output)[0]["conversation_id"] == "group_stable_id"


def test_ambiguous_noninteractive_selection_never_exports_first_chat(cli_environment):
    env = cli_environment
    env.engine.list_conversations = lambda *args, **kwargs: [
        Conversation("id_1", "Same name", "contact"), Conversation("id_2", "Same name", "contact")]
    code, output, error = env.invoke(["export", "--query", "Same name"])
    assert code == 2 and not output and not env.calls
    assert "--conversation-id" in error


def test_interactive_duplicate_names_show_stable_ids_and_export_chosen_chat(cli_environment):
    env = cli_environment
    env.engine.list_conversations = lambda *args, **kwargs: [
        Conversation("id_1", "Same name", "contact"), Conversation("id_2", "Same name", "group")]
    code, _, error = env.invoke(["export", "--query", "Same"], TTY("2\n"))
    assert code == 0
    assert env.calls[0][1] == "id_2"
    assert "[id_1]" in error and "[id_2]" in error


def test_multiple_accounts_require_choice_and_do_not_persist_interactive_choice(cli_environment):
    env = cli_environment
    save_config(replace(env.config, account_id=None), env.path)
    env.engine.discover_accounts = lambda root: [Account("account_a", root / "account_a"), Account("account_b", root / "account_b")]
    code, _, error = env.invoke(["export", "--conversation-id", "stable_id"])
    assert code == 2 and "--account" in error and not env.calls
    code, _, _ = env.invoke(["export", "--conversation-id", "stable_id"], TTY("2\n"))
    assert code == 0 and env.calls[0][0].account_id == "account_b"
    assert load_config(env.path).account_id is None


def test_global_options_work_after_subcommands_and_config_set_uses_flags(cli_environment):
    env = cli_environment
    code, _, _ = env.invoke(["config", "set", "--account", "another_synthetic_account", "--ffmpeg", str(env.tmp / "ffmpeg.exe")])
    assert code == 0
    settings = load_config(env.path)
    assert settings.account_id == "another_synthetic_account" and settings.ffmpeg == env.tmp / "ffmpeg.exe"
    assert env.invoke(["config", "set", "--clear-account"])[0] == 0
    assert load_config(env.path).account_id is None


def test_capture_is_delegated_with_explicit_scoping_and_redacts_key_fields(cli_environment):
    env = cli_environment
    captured = []

    def capture(settings, **kwargs):
        captured.append(kwargs)
        return {"completed": True, "database_keys": {"salt": "synthetic_secret"}, "nested": {"aes": "synthetic_secret"}}

    env.backend.capture_keys = capture
    probe = env.tmp / "ciphertext-only.json"
    code, output, error = env.invoke(["capture", "--timeout", "45", "--pid", "1234", "--images-probe", str(probe)])
    assert code == 0 and not error
    assert {key: value for key, value in captured[0].items() if key != "progress_cb"} == {
        "timeout": 45, "pid": 1234, "images_probe": probe}
    assert callable(captured[0]["progress_cb"])
    assert "synthetic_secret" not in output
    assert "[redacted]" in output


def test_capture_can_prepare_only_selected_conversation_image_probe(cli_environment):
    env = cli_environment
    selected = []
    capture_calls = []
    generated = env.tmp / "generated-ciphertext-only.json"

    def prepare(settings, conversation_id):
        selected.append((settings.account_id, conversation_id))
        return generated

    env.backend.prepare_image_probe = prepare
    env.backend.capture_keys = lambda settings, **kwargs: capture_calls.append(kwargs) or {"returncode": 0}
    code, _, error = env.invoke(["capture", "--conversation-id", "selected_group_id"])
    assert code == 0 and not error
    assert selected == [("synthetic_account", "selected_group_id")]
    assert capture_calls[0]["images_probe"] == generated


def test_capture_nonzero_returncode_is_reported_as_failure(cli_environment):
    cli_environment.backend.capture_keys = lambda *args, **kwargs: {"returncode": 7, "status": "failed"}
    code, output, _ = cli_environment.invoke(["capture"])
    assert code == 1 and json.loads(output)["returncode"] == 7


@pytest.mark.parametrize("arguments", [
    ["capture", "--timeout", "601"], ["capture", "--timeout", "0"], ["capture", "--pid", "0"],
    ["capture", "--pid", "4294967296"], ["export", "--master-key", "synthetic_secret"],
    ["capture", "--images-probe", "probe.json", "--conversation-id", "stable_id"],
])
def test_invalid_cli_values_never_echo_potential_secrets(cli_environment, arguments):
    code, output, error = cli_environment.invoke(arguments)
    assert code == 2 and not output
    assert "synthetic_secret" not in error


def test_backend_exception_text_is_not_logged(cli_environment):
    def failure(*args, **kwargs):
        raise RuntimeError("key=synthetic_secret; private message body")

    cli_environment.engine.export_conversation = failure
    code, output, error = cli_environment.invoke(["export", "--conversation-id", "stable_id"])
    assert code == 1 and not output
    assert "synthetic_secret" not in error and "private message" not in error
    assert "RuntimeError" in error


def test_untrusted_contact_display_cannot_inject_terminal_escape(cli_environment):
    cli_environment.engine.list_conversations = lambda *args, **kwargs: [
        Conversation("stable_id", "name\x1b[31m\nnext", "contact")]
    code, output, _ = cli_environment.invoke(["conversations"])
    assert code == 0 and "\x1b" not in output


def test_doctor_does_not_import_engine_or_scan_accounts(cli_environment, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("doctor must not load the database engine")
    monkeypatch.setattr("wechat_export_tool.cli.importlib.import_module", forbidden)
    code, output, error = cli_environment.invoke(["doctor", "--json"])
    report = json.loads(output)
    assert code == 0 and not error
    assert report["version"] == "synthetic-version"
    assert report["tools"]["capture"]["available"] is False
    assert report["json_contract"]["stdout"] == "one_json_value"
    assert report["json_contract"]["encoding"] == "utf-8"
    assert report["json_contract"]["interactive"] is False
    assert "export" in report["commands"]


@pytest.mark.parametrize("arguments,exit_code,error_code", [
    (["export", "--master-key", "synthetic_secret", "--json"], 2, "invalid_arguments"),
    (["--json", "capture", "--timeout", "601"], 2, "invalid_arguments"),
])
def test_json_usage_errors_are_single_safe_machine_readable_results(cli_environment, arguments, exit_code, error_code):
    code, output, error = cli_environment.invoke(arguments)
    result = json.loads(output)
    assert code == exit_code and not error
    assert result["ok"] is False and result["schema_version"] == 1
    assert result["error"]["code"] == error_code
    assert "synthetic_secret" not in output


def test_json_operation_error_and_cancellation_are_machine_readable(cli_environment):
    env = cli_environment
    def failure(*args, **kwargs):
        raise RuntimeError("synthetic_secret")
    env.engine.export_conversation = failure
    code, output, error = env.invoke(["--json", "export", "--conversation-id", "stable_id"])
    assert code == 1 and not error
    assert json.loads(output)["error"]["code"] == "operation_failed"
    assert "synthetic_secret" not in output
    def cancel(*args, **kwargs):
        raise KeyboardInterrupt
    env.backend.capture_keys = cancel
    code, output, error = env.invoke(["capture", "--json"])
    assert code == 130 and not error
    assert json.loads(output)["error"]["code"] == "cancelled"


def test_json_capture_progress_stays_on_stderr_and_result_is_single_json(cli_environment):
    def capture(settings, **kwargs):
        kwargs["progress_cb"]({"state": "watching_login", "key": "synthetic_secret"})
        return {"returncode": 0, "status": "completed"}
    cli_environment.backend.capture_keys = capture
    code, output, error = cli_environment.invoke(["capture", "--json"])
    assert code == 0
    assert json.loads(output) == {"returncode": 0, "status": "completed"}
    assert json.loads(error)["capture"]["state"] == "watching_login"
    assert "synthetic_secret" not in error


def test_config_set_does_not_persist_portable_bundle_paths(cli_environment):
    runtime = sys.modules["wechat_export_tool.runtime"]
    stripped = []
    def portable_settings(settings):
        stripped.append(settings)
        return replace(settings, capture_exe=None)
    runtime.without_bundled_defaults = portable_settings
    code, output, error = cli_environment.invoke(["config", "set", "--capture-exe",
        str(cli_environment.tmp / "portable/runtime/capture/wxmori-media.exe")])
    assert code == 0 and not error and stripped
    assert load_config(cli_environment.path).capture_exe is None


def test_real_console_pipes_use_utf8_and_injected_streams_are_untouched(monkeypatch):
    class Pipe(io.StringIO):
        def __init__(self):
            super().__init__()
            self.settings = []
        def reconfigure(self, **settings):
            self.settings.append(settings)
    pipe_out, pipe_error = Pipe(), Pipe()
    monkeypatch.setattr(sys, "stdout", pipe_out)
    monkeypatch.setattr(sys, "stderr", pipe_error)
    assert main(["--help"]) == 0
    assert pipe_out.settings == pipe_error.settings == [{"encoding": "utf-8", "errors": "replace"}]
    pipe_out.settings.clear(); pipe_error.settings.clear()
    assert main(["--help"], stdout=io.StringIO(), stderr=io.StringIO()) == 0
    assert pipe_out.settings == pipe_error.settings == []
