"""Exercise a portable release against generated encrypted databases only.

This maintainer test creates its own account tree and DPAPI keyring below one
temporary directory. It never captures a key, opens a real account, or starts
WeChat. Encryption is the inverse of the pinned chatlog-keeper SQLCipher-v4
reader: 4096-byte pages, 80-byte reserve, AES-CBC and per-page HMAC-SHA512.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import hmac
import io
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from Crypto.Cipher import AES
from PIL import Image

from wechat_export_tool.backend import account_fingerprint, keyring_path
from wechat_export_tool.config import ExportConfig, save_config
from wechat_export_tool.security import private_text_writer, protect_bytes

ACCOUNT = "synthetic_self"
CONTACT = "synthetic_contact"
GROUP = "synthetic_group@chatroom"
OTHER = "synthetic_other"
STAMP = 1735729200  # 2025-01-01, Asia/Shanghai.


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _blob(value: bytes) -> str:
    return "X'" + value.hex() + "'"


def _sqlite_library():
    if os.name == "nt":
        filename = Path(sys.base_prefix) / "DLLs/sqlite3.dll"
        if not filename.is_file():
            raise RuntimeError("maintainer_sqlite_library_missing")
        library = ctypes.CDLL(str(filename))
    else:
        filename = ctypes.util.find_library("sqlite3")
        if not filename:
            raise RuntimeError("maintainer_sqlite_library_missing")
        library = ctypes.CDLL(filename)
    library.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    library.sqlite3_open.restype = ctypes.c_int
    library.sqlite3_close.argtypes = [ctypes.c_void_p]
    library.sqlite3_close.restype = ctypes.c_int
    library.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p]
    library.sqlite3_exec.restype = ctypes.c_int
    library.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    library.sqlite3_file_control.restype = ctypes.c_int
    return library


def create_reserved_sqlite(path: Path, sql: str) -> None:
    """Make a normal SQLite DB whose pages leave room for cipher metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    library = _sqlite_library()
    handle = ctypes.c_void_p()
    if library.sqlite3_open(str(path).encode("utf-8"), ctypes.byref(handle)):
        raise RuntimeError("synthetic_database_open_failed")
    try:
        if library.sqlite3_exec(handle, b"PRAGMA page_size=4096;", None, None, None):
            raise RuntimeError("synthetic_database_page_size_failed")
        reserve = ctypes.c_int(80)
        if library.sqlite3_file_control(handle, b"main", 38, ctypes.byref(reserve)):
            raise RuntimeError("synthetic_database_reserve_failed")
        if library.sqlite3_exec(handle, ("VACUUM;" + sql).encode("utf-8"), None, None, None):
            raise RuntimeError("synthetic_database_sql_failed")
    finally:
        library.sqlite3_close(handle)
    data = path.read_bytes()
    if data[:16] != b"SQLite format 3\0" or data[20] != 80 or len(data) % 4096:
        raise RuntimeError("synthetic_database_layout_invalid")
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("synthetic_database_integrity_failed")


def encrypt_sqlite(source: Path, destination: Path, *, label: str) -> tuple[bytes, bytes]:
    """Generate deterministic *test* key/salt; neither is a user's secret."""
    key = hashlib.sha256(("synthetic-key:" + label).encode()).digest()
    salt = hashlib.sha256(("synthetic-salt:" + label).encode()).digest()[:16]
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, 32)
    raw = source.read_bytes()
    pages = []
    for offset in range(0, len(raw), 4096):
        number = offset // 4096 + 1
        page = raw[offset:offset + 4096]
        prefix = 16 if number == 1 else 0
        iv = hashlib.sha256(f"synthetic-iv:{label}:{number}".encode()).digest()[:16]
        body = AES.new(key, AES.MODE_CBC, iv).encrypt(page[prefix:4016]) + iv
        tag = hmac.new(mac_key, body + struct.pack("<I", number), hashlib.sha512).digest()
        pages.append((salt if prefix else b"") + body + tag)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"".join(pages))
    return salt, key


def _table(identifier: str) -> str:
    return "Msg_" + hashlib.md5(identifier.encode()).hexdigest()


def child_environment(*, isolate_runtime: bool = False) -> dict[str, str]:
    """Isolate bundle children, without changing the maintainer interpreter.

    This is PATH isolation, not an assertion that host software was uninstalled.
    The Python maintainer process still creates the generated encrypted fixture.
    """
    blocked = {"PYTHONHOME", "PYTHONPATH", "GOROOT", "GOPATH", "GOBIN", "GOTOOLDIR",
               "NODE_PATH", "NODE_OPTIONS", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV"}
    if isolate_runtime:
        blocked.add("PATH")
    environment = {key: value for key, value in os.environ.items() if key.upper() not in blocked}
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if isolate_runtime:
        windows = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "C:/Windows")
        environment["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
    return environment


def _run(arguments: list[str], *, cwd: Path, timeout: int = 90,
         environment: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    environment = child_environment() if environment is None else environment
    result = subprocess.run(arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=timeout)
    if result.returncode:
        # Only a fixed diagnostic: subprocess text could contain local paths.
        raise RuntimeError("synthetic_command_failed_" + str(result.returncode))
    return result


def _json_command(cli: list[str], arguments: list[str], root: Path, *, environment=None):
    result = _run(cli + arguments, cwd=root, environment=environment)
    try:
        return json.loads(result.stdout.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("cli_stdout_is_not_one_utf8_json_document") from exc


def prepare_fixture(root: Path, doctor: dict, *, environment=None) -> ExportConfig:
    account = root / "data" / ACCOUNT
    plain = root / "plain"
    plain.mkdir()
    contact_sql = "CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT,alias TEXT,local_type INTEGER);"
    for identifier, title, kind in [(ACCOUNT, "Synthetic self", 1),
                                     (CONTACT, "Shared synthetic display", 1),
                                     (GROUP, "Shared synthetic display", 2),
                                     (OTHER, "Other synthetic contact", 1)]:
        contact_sql += f"INSERT INTO contact VALUES({_quote(identifier)},{_quote(title)},'','',{kind});"
    contact_plain = plain / "contact.db"
    create_reserved_sqlite(contact_plain, contact_sql)

    image_id = "a" * 32
    picture = io.BytesIO()
    Image.new("RGB", (12, 8), (23, 155, 77)).save(picture, format="PNG")
    image_path = account / "msg/attach" / hashlib.md5(CONTACT.encode()).hexdigest() / "2025-01/Img" / (image_id + ".dat")
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(picture.getvalue())
    payload = b"Synthetic release smoke attachment.\n"
    file_path = account / "msg/file/2025-01/smoke.txt"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(payload)
    image_xml = '<msg><img md5="' + "b" * 32 + '" /></msg>'
    file_xml = ("<msg><appmsg><title>smoke.txt</title><type>6</type><appattach>"
                f"<totallen>{len(payload)}</totallen><md5>{hashlib.md5(payload).hexdigest()}</md5>"
                "<fileext>txt</fileext></appattach></appmsg></msg>")
    rows = [(CONTACT, 101, 1, CONTACT, "Synthetic selected contact text", None),
            (CONTACT, 102, 3, CONTACT, image_xml, b"\x08\x01\x10\x02\x1a\x22" + image_id.encode()),
            (CONTACT, 103, 49, CONTACT, file_xml, None),
            (GROUP, 201, 1, CONTACT, "Synthetic selected group text", None),
            (OTHER, 999, 1, OTHER, "FORBIDDEN_OTHER_SYNTHETIC_CONVERSATION", None)]
    voice = doctor.get("capabilities", {}).get("voice_decode", False)
    silk_path = root / "generated.silk"
    if voice:
        tools = doctor["tools"]
        script = ("const fs=require('node:fs');const silk=require(process.argv[1]);"
                  "silk.encode(Buffer.alloc(48000),24000).then(r=>fs.writeFileSync(process.argv[2],r.data))"
                  ".catch(()=>process.exitCode=1)")
        _run([tools["node"]["path"], "-e", script, tools["silk"]["path"], str(silk_path)],
             cwd=root, environment=environment)
        rows.append((CONTACT, 104, 34, CONTACT,
                     '<msg><voicemsg voicelength="1000" /></msg>', None))
    message_sql = "CREATE TABLE Name2Id(user_name TEXT);"
    for identifier in (CONTACT, GROUP, OTHER):
        message_sql += f"INSERT INTO Name2Id VALUES({_quote(identifier)});"
        message_sql += (f'CREATE TABLE "{_table(identifier)}"(local_id INTEGER PRIMARY KEY,server_id INTEGER,'
                        'local_type INTEGER,sort_seq INTEGER,real_sender_id INTEGER,create_time INTEGER,'
                        'message_content BLOB,packed_info_data BLOB);')
    for local, (identifier, server, kind, sender, body, packed) in enumerate(rows, 1):
        sender_id = (CONTACT, GROUP, OTHER).index(sender) + 1
        message_sql += (f'INSERT INTO "{_table(identifier)}" VALUES({local},{server},{kind},{local},'
                        f'{sender_id},{STAMP},{_blob(body.encode())},{_blob(packed) if packed else "NULL"});')
    message_plain = plain / "message.db"
    create_reserved_sqlite(message_plain, message_sql)
    databases = [(contact_plain, account / "db_storage/contact/contact.db"),
                 (message_plain, account / "db_storage/message/message_0.db")]
    if voice:
        media_plain = plain / "media.db"
        local = len(rows)
        media_sql = ("CREATE TABLE Name2Id(user_name TEXT);"
                     f"INSERT INTO Name2Id VALUES({_quote(CONTACT)});"
                     "CREATE TABLE VoiceInfo(chat_name_id INTEGER,create_time INTEGER,local_id INTEGER,"
                     "svr_id INTEGER,voice_data BLOB,data_index INTEGER);"
                     f"INSERT INTO VoiceInfo VALUES(1,{STAMP},{local},104,{_blob(silk_path.read_bytes())},0);")
        create_reserved_sqlite(media_plain, media_sql)
        databases.append((media_plain, account / "db_storage/message/media_0.db"))
    config = ExportConfig(data_root=root / "data", account_id=ACCOUNT, cache_dir=root / "cache")
    material = {}
    for number, (source, destination) in enumerate(databases):
        salt, key = encrypt_sqlite(source, destination, label=str(number))
        material[salt.hex()] = key.hex()
    envelope = {"format": "wxmori.dpapi.v1", "protected": protect_bytes(json.dumps({
        "version": 1, "account": account_fingerprint(config), "database_keys": material,
    }).encode())}
    with private_text_writer(keyring_path(config)) as handle:
        json.dump(envelope, handle)
    save_config(config, root / "config.json")
    return config


def exercise(cli: list[str], root: Path, gui: Path | None = None, *, isolate_runtime=False) -> dict:
    environment = child_environment(isolate_runtime=isolate_runtime)
    _run(cli + ["--help"], cwd=root, environment=environment)
    # Explicit absent config prevents reading a user's saved settings in doctor.
    doctor = _json_command(cli, ["--config", str(root / "absent.json"), "doctor", "--json"], root,
                           environment=environment)
    if doctor.get("schema_version") != 1:
        raise RuntimeError("doctor_schema_invalid")
    if isolate_runtime:
        for name in ("capture", "node", "silk"):
            tool = doctor.get("tools", {}).get(name, {})
            if not tool.get("available") or not tool.get("bundled"):
                raise RuntimeError("required_tool_not_bundled_" + name)
    config = prepare_fixture(root, doctor, environment=environment)
    common = ["--config", str(root / "config.json"), "--json"]
    accounts = _json_command(cli, common + ["accounts"], root, environment=environment)
    if {item["account_id"] for item in accounts} != {ACCOUNT}:
        raise RuntimeError("synthetic_account_isolation_failed")
    conversations = _json_command(cli, common + ["conversations", "--query", "Shared synthetic"], root,
                                  environment=environment)
    if {item["conversation_id"] for item in conversations} != {CONTACT, GROUP}:
        raise RuntimeError("synthetic_same_name_selection_failed")
    exports = {}
    for identifier, label, expected in [(CONTACT, "contact", 4 if doctor["capabilities"]["voice_decode"] else 3),
                                         (GROUP, "group", 1)]:
        output = root / ("export-" + label)
        result = _json_command(cli, common + ["export", "--conversation-id", identifier,
                                             "--output", str(output)], root, environment=environment)
        messages = json.loads((output / "messages.json").read_text(encoding="utf-8"))
        if not result.get("verified") or result.get("messages") != expected:
            raise RuntimeError("synthetic_export_not_verified")
        if {item["conversation_id"] for item in messages} != {identifier}:
            raise RuntimeError("synthetic_conversation_isolation_failed")
        for name in ("messages.json", "raw_messages.jsonl", "index.html"):
            if "FORBIDDEN_OTHER_SYNTHETIC_CONVERSATION" in (output / name).read_text(encoding="utf-8"):
                raise RuntimeError("synthetic_foreign_conversation_leaked")
        if label == "contact":
            kinds = {medium["kind"] for message in messages for medium in message["media"]
                     if medium["status"] == "available"}
            required = {"image", "file"} | ({"audio"} if doctor["capabilities"]["voice_decode"] else set())
            if not required.issubset(kinds):
                raise RuntimeError("synthetic_attachment_missing")
        exports[label] = {"verified": True, "messages": expected, "counts": result["counts"]}
    error = subprocess.run(cli + common + ["export", "--conversation-id", "synthetic_missing",
                                          "--output", str(root / "invalid-export")],
                           cwd=root, stdin=subprocess.DEVNULL, capture_output=True, timeout=90,
                           env=environment)
    try:
        error_json = json.loads(error.stdout.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("cli_error_not_machine_readable") from exc
    if error.returncode == 0 or not isinstance(error_json, dict):
        raise RuntimeError("cli_error_contract_failed")
    report = {"synthetic": True, "encrypted_sqlite": True, "dpapi_keyring": True,
              "cli": True, "voice_wasm": bool(doctor["capabilities"]["voice_decode"]), "exports": exports,
              "no_external_python_go_node": bool(isolate_runtime),
              "runtime_test_boundary": "Bundle children use a system-only PATH; host runtimes are not uninstalled. "
                                       "The maintainer fixture generator itself uses Python."}
    if gui:
        gui_report = root / "gui-report.json"
        _run([str(gui), "--demo", "--smoke-test", "--output", str(root / "gui-exports"),
              "--smoke-report", str(gui_report)], cwd=root, timeout=75, environment=environment)
        status = json.loads(gui_report.read_text(encoding="utf-8"))
        if not status.get("success") or not status.get("verified"):
            raise RuntimeError("gui_synthetic_smoke_failed")
        if not (Path(status["output_directory"]) / "index.html").is_file():
            raise RuntimeError("gui_synthetic_html_missing")
        report["gui"] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--bundle", type=Path, help="Portable directory containing both executables")
    choice.add_argument("--source", action="store_true", help="Use this interpreter and installed source CLI")
    parser.add_argument("--skip-gui", action="store_true", help="Only exercise the console CLI")
    parser.add_argument("--report", type=Path, help="Optional synthetic-only JSON result")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("Windows is required for the synthetic DPAPI fixture.")
    gui = None
    if args.bundle:
        bundle = args.bundle.resolve(strict=True)
        cli = [str(bundle / "wechat-export.exe")]
        if not Path(cli[0]).is_file(): parser.error("wechat-export.exe is missing.")
        if not args.skip_gui:
            gui = bundle / "WeChatExportTool.exe"
            if not gui.is_file(): parser.error("WeChatExportTool.exe is missing.")
    else:
        cli = [sys.executable, "-m", "wechat_export_tool"]
    with tempfile.TemporaryDirectory(prefix="synthetic-bundle-smoke-") as temporary:
        report = exercise(cli, Path(temporary).resolve(), gui, isolate_runtime=bool(args.bundle))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"synthetic": True, "success": False, "error": type(exc).__name__,
                          "code": str(exc) if isinstance(exc, RuntimeError) else "smoke_failed"}))
        raise SystemExit(1)
