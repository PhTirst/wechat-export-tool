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
    monkeypatch.setitem(sys.modules, "wechat_export_tool.engine", engine)
    monkeypatch.setitem(sys.modules, "wechat_export_tool.backend", backend)

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
    assert captured == [{"timeout": 45, "pid": 1234, "images_probe": probe}]
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
