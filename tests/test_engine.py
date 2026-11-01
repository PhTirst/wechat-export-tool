"""Synthetic SQLite -> real collectors -> offline viewer/verification tests.

Only the encryption boundary is replaced. All source and output files are
created under pytest's temporary directory; no user account, keyring, existing
export, external cache, network, or client process is consulted.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import zstandard
from PIL import Image

from wechat_export_tool import engine
from wechat_export_tool.config import ExportConfig
from wechat_export_tool.errors import ExportError
from wechat_export_tool.verify_media_export import verify


TZ = timezone(timedelta(hours=8))
STAMP = int(datetime(2026, 10, 12, 9, 30, tzinfo=TZ).timestamp())
SELF = "wxid_synth_self"
DIRECT = "synthetic_contact_a"
OTHER = "synthetic_contact_b"
GROUP = "synthetic_room@chatroom"
EMPTY = "synthetic_empty_contact"


def _table(conversation: str) -> str:
    return "Msg_" + hashlib.md5(conversation.encode()).hexdigest()


@pytest.fixture
def synthetic_account(tmp_path, monkeypatch):
    root = tmp_path / "data"
    account = root / (SELF + "_device")
    contact = account / "db_storage/contact/contact.db"
    contact.parent.mkdir(parents=True)
    with closing(sqlite3.connect(contact)) as connection:
        connection.execute("CREATE TABLE contact(username TEXT PRIMARY KEY,remark TEXT,nick_name TEXT,alias TEXT,local_type INTEGER)")
        connection.executemany("INSERT INTO contact VALUES(?,?,?,?,?)", [
            (SELF, "Synthetic self", "", "self-alias", 1),
            (DIRECT, "Shared display", "First nickname", "first-alias", 1),
            (OTHER, "Shared display", "Second nickname", "second-alias", 1),
            (GROUP, "Shared display", "Synthetic group", "", 2),
            (EMPTY, "Empty synthetic contact", "", "empty-alias", 1),
            ("synthetic_member_a", "Member A", "", "", 1),
            ("synthetic_member_b", "Member B", "", "", 1),
        ])
        connection.commit()
    message_root = account / "db_storage/message"
    message_root.mkdir()
    decrypt_requests = []

    def shard(number: int) -> Path:
        path = message_root / f"message_{number}.db"
        if not path.exists():
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
                connection.commit()
        return path

    def add(conversation=DIRECT, *, server=1001, kind=1, sender=DIRECT, number=0,
            sender_id=1, timestamp=STAMP, sort_seq=1, body="Synthetic selected text",
            packed=None, local_id=None):
        path = shard(number)
        name = _table(conversation)
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f'CREATE TABLE IF NOT EXISTS "{name}"('
                "local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,sort_seq INTEGER,"
                "real_sender_id INTEGER,create_time INTEGER,message_content BLOB,packed_info_data BLOB)")
            existing = connection.execute("SELECT user_name FROM Name2Id WHERE rowid=?", (sender_id,)).fetchone()
            if existing is None:
                connection.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", (sender_id, sender))
            else:
                assert existing[0] == sender, "Synthetic shard sender setup conflict"
            connection.execute(f'INSERT INTO "{name}" VALUES(?,?,?,?,?,?,?,?)',
                (local_id, server, kind, sort_seq, sender_id, timestamp, body, packed))
            connection.commit()
        return path

    @contextmanager
    def decrypted(sources, keys):
        sources = [Path(source) for source in sources]
        assert all(source.resolve().is_relative_to(account.resolve()) for source in sources)
        assert all(source.is_file() for source in sources)
        decrypt_requests.append(tuple(source.name for source in sources))
        yield {source: source for source in sources}

    def forbid_process(*args, **kwargs):
        raise AssertionError("Synthetic engine tests must not start a process")

    monkeypatch.setattr(engine, "_decrypted", decrypted)
    monkeypatch.setattr(engine, "load_page_keys", lambda config: {b"s" * 16: b"k" * 32})
    monkeypatch.setattr(subprocess, "run", forbid_process)
    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    config = ExportConfig(data_root=root, account_id=account.name, cache_dir=tmp_path / "cache")
    shard(0)
    return SimpleNamespace(config=config, account=account, root=root, contact=contact,
                           add=add, shard=shard, message_root=message_root,
                           requests=decrypt_requests, output=tmp_path / "export")


def _read_export(path: Path):
    messages = json.loads((path / "messages.json").read_text(encoding="utf-8"))
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    raw = [json.loads(line) for line in (path / "raw_messages.jsonl").read_text(encoding="utf-8").splitlines()]
    return messages, manifest, raw


def test_discovers_only_account_directories(synthetic_account):
    sample = synthetic_account
    (sample.root / "another_synthetic_account/db_storage").mkdir(parents=True)
    (sample.root / "not-an-account").mkdir()
    found = engine.discover_accounts(sample.root)
    assert {item.account_id for item in found} == {sample.config.account_id, "another_synthetic_account"}
    assert engine.discover_accounts(sample.root / "missing") == []


def test_contact_and_group_queries_preserve_same_name_distinct_ids(synthetic_account):
    sample = synthetic_account
    contacts = engine.list_conversations(sample.config, query="shared display", kind="contact")
    assert {item.conversation_id for item in contacts} == {DIRECT, OTHER}
    assert {item.display_name for item in contacts} == {"Shared display"}
    groups = engine.list_conversations(sample.config, query="shared display", kind="group")
    assert [item.conversation_id for item in groups] == [GROUP]
    assert engine.list_conversations(sample.config, query="first-alias")[0].conversation_id == DIRECT
    assert engine.list_conversations(sample.config, query="Second nickname")[0].conversation_id == OTHER
    assert engine.list_conversations(sample.config, query="not a synthetic contact") == []
    with pytest.raises(ExportError, match="conversation_kind_invalid"):
        engine.list_conversations(sample.config, kind="everything")


def test_zero_message_contact_remains_listed_but_export_creates_no_partial_output(synthetic_account):
    sample = synthetic_account
    assert [item.conversation_id for item in engine.list_conversations(sample.config, query="Empty synthetic")] == [EMPTY]
    with pytest.raises(ExportError, match="conversation_has_no_local_messages"):
        engine.export_conversation(sample.config, EMPTY, sample.output)
    assert not sample.output.exists()


def test_export_isolates_selected_stable_id_even_when_display_names_match(synthetic_account):
    sample = synthetic_account
    sample.add(DIRECT, server=1001, body="Only selected synthetic conversation")
    # Deliberately reuse the same server ID in another table. The selected
    # conversation's exact table must remain the only source of exported rows.
    sample.add(OTHER, server=1001, sender=OTHER, sender_id=2, body="FORBIDDEN_OTHER_CONVERSATION")
    sample.add(GROUP, server=1002, sender="synthetic_member_a", sender_id=3, body="FORBIDDEN_GROUP_CONVERSATION")
    result = engine.export_conversation(sample.config, DIRECT, sample.output)
    messages, manifest, raw = _read_export(sample.output)
    assert result["verified"] and result["messages"] == 1
    assert len(raw) == manifest["raw_rows"] == manifest["messages"] == 1
    assert {row["conversation_id"] for row in raw} == {DIRECT}
    assert {message["conversation_id"] for message in messages} == {DIRECT}
    for path in (sample.output / "messages.json", sample.output / "raw_messages.jsonl", sample.output / "index.html"):
        contents = path.read_text(encoding="utf-8")
        assert "FORBIDDEN_OTHER_CONVERSATION" not in contents
        assert "FORBIDDEN_GROUP_CONVERSATION" not in contents
    assert verify(sample.output, expect_count=1, expect_target="Shared display", deep=True)["verified"]


def test_unknown_message_type_is_lossless_in_raw_and_visible_as_placeholder(synthetic_account):
    sample = synthetic_account
    original = b"\x00\xff\x01synthetic unknown binary\x00"
    sample.add(server=2001, kind=65000, body=original)
    result = engine.export_conversation(sample.config, DIRECT, sample.output)
    messages, manifest, raw = _read_export(sample.output)
    assert result["verified"] and len(messages) == len(raw) == 1
    assert messages[0]["msg_type"] == 65000 and messages[0]["parse_status"] == "unparsed"
    assert "65000" in messages[0]["content"] and "raw_messages.jsonl" in messages[0]["content"]
    assert manifest["unparsed_by_type"] == {"65000": 1}
    assert base64.b64decode(raw[0]["row"]["message_content"]["$bytes_base64"]) == original
    assert raw[0]["row"]["local_type"] == 65000


def test_all_message_shards_and_all_years_are_discovered_and_globally_sorted(synthetic_account):
    sample = synthetic_account
    old = int(datetime(2013, 1, 2, tzinfo=TZ).timestamp())
    future = int(datetime(2028, 1, 2, tzinfo=TZ).timestamp())
    sample.add(number=0, server=3003, timestamp=STAMP, sort_seq=30)
    sample.add(number=2, server=3001, timestamp=STAMP, sort_seq=10)
    sample.add(number=17, server=3002, timestamp=STAMP, sort_seq=20)
    sample.add(number=101, server=3000, timestamp=old)
    sample.add(number=901, server=3004, timestamp=future)
    (sample.message_root / "message_preview.db").write_bytes(b"not a message shard")
    (sample.message_root / "message_resource.db").write_bytes(b"not a message shard")
    engine.export_conversation(sample.config, DIRECT, sample.output)
    messages, manifest, raw = _read_export(sample.output)
    assert [message["server_id"] for message in messages] == ["3000", "3001", "3002", "3003", "3004"]
    assert manifest["raw_rows"] == 5 and len(raw) == 5
    assert set(sample.requests[0]) == {"contact.db", "message_0.db", "message_2.db", "message_17.db", "message_101.db", "message_901.db"}
    assert {row["source_shard"] for row in raw} == {f"message_{number}.db" for number in (0, 2, 17, 101, 901)}


def test_group_sender_resolution_is_per_shard_and_includes_all_members(synthetic_account):
    sample = synthetic_account
    # The same Name2Id row number intentionally denotes different members in
    # different shards; cross-shard integer identity reuse would misattribute B.
    sample.add(GROUP, number=0, server=4001, sender="synthetic_member_a", sender_id=1, sort_seq=1,
               body="synthetic_member_a:\nMember A selected text")
    sample.add(GROUP, number=9, server=4002, sender="synthetic_member_b", sender_id=1, sort_seq=2,
               body="Member B selected text")
    sample.add(GROUP, number=9, server=4003, sender=SELF, sender_id=2, sort_seq=3,
               body="Synthetic self text")
    engine.export_conversation(sample.config, GROUP, sample.output)
    messages, _, _ = _read_export(sample.output)
    assert [message["sender"] for message in messages] == ["Member A", "Member B", "我"]
    assert [message["sender_wxid"] for message in messages] == ["synthetic_member_a", "synthetic_member_b", SELF]
    assert [message["is_self"] for message in messages] == [False, False, True]
    assert all(message["is_group_chat"] and message["conversation_id"] == GROUP for message in messages)
    assert messages[0]["content"] == "Member A selected text"


def test_zstd_xml_nul_padding_recovers_quote_and_real_file_attachment(synthetic_account):
    sample = synthetic_account
    payload = b"Synthetic checked attachment\n\x00binary allowed\n"
    checksum = hashlib.md5(payload).hexdigest()
    filename = "synthetic_checked_document.bin"
    source = sample.account / "msg/file/2026-10" / filename
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    file_xml = (f"<msg><appmsg><title>{filename}</title><type>6</type><appattach>"
                f"<totallen>{len(payload)}</totallen><md5>{checksum}</md5><fileext>bin</fileext>"
                "</appattach></appmsg></msg>\x00\r\n\t ")
    quote_xml = ("<msg><appmsg><title>Synthetic recovered reply</title><type>57</type><refermsg>"
                 "<displayname>Member A</displayname><content>Synthetic quoted text</content><type>1</type>"
                 "</refermsg></appmsg></msg>\x00")
    compressed = zstandard.ZstdCompressor().compress(file_xml.encode())
    sample.add(server=5001, kind=(6 << 32) | 49, body=compressed, sort_seq=1)
    sample.add(server=5002, kind=49, body=zstandard.ZstdCompressor().compress(quote_xml.encode()), sort_seq=2)
    result = engine.export_conversation(sample.config, DIRECT, sample.output)
    messages, manifest, raw = _read_export(sample.output)
    assert result["verified"] and manifest["unparsed_by_type"] == {}
    assert all(message["msg_type"] == 49 and message["parse_status"] == "parsed" for message in messages)
    assert messages[0]["attachment_meta"]["kind"] == "file"
    item = messages[0]["media"][0]
    assert item["kind"] == "file" and item["status"] == "available"
    assert (sample.output / item["path"]).read_bytes() == payload
    assert "Synthetic recovered reply" in messages[1]["content"]
    assert "引用" in messages[1]["content"]
    assert base64.b64decode(raw[0]["row"]["message_content"]["$bytes_base64"]) == compressed
    assert raw[0]["row"]["local_type"] == (6 << 32) | 49
    assert verify(sample.output, expect_count=2, deep=True)["verified"]


def test_real_png_and_file_flow_through_collectors_and_viewer_verifier(synthetic_account):
    sample = synthetic_account
    image_id = "a" * 32
    stream = io.BytesIO()
    Image.new("RGB", (9, 7), (11, 220, 33)).save(stream, "PNG")
    png = stream.getvalue()
    month = "2026-10"
    image_folder = sample.account / "msg/attach" / hashlib.md5(DIRECT.encode()).hexdigest() / month / "Img"
    image_folder.mkdir(parents=True)
    (image_folder / (image_id + ".dat")).write_bytes(png)
    wrong_folder = sample.account / "msg/attach" / hashlib.md5(OTHER.encode()).hexdigest() / month / "Img"
    wrong_folder.mkdir(parents=True)
    (wrong_folder / (image_id + ".dat")).write_bytes(b"FORBIDDEN_OTHER_IMAGE")
    image_xml = '<msg><img md5="' + "b" * 32 + '" aeskey="' + "01" * 16 + '" length="81" /></msg>\x00'
    sample.add(server=6001, kind=3, body=image_xml.encode(), packed=b"\x08\x01\x10\x02\x1a\x22" + image_id.encode())
    file_payload = b"Synthetic downloadable document"
    file_source = sample.account / "msg/file" / month / "synthetic.txt"
    file_source.parent.mkdir(parents=True)
    file_source.write_bytes(file_payload)
    xml = ("<msg><appmsg><title>synthetic.txt</title><type>6</type><appattach>"
           f"<totallen>{len(file_payload)}</totallen><md5>{hashlib.md5(file_payload).hexdigest()}</md5>"
           "<fileext>txt</fileext></appattach></appmsg></msg>\x00")
    sample.add(server=6002, kind=49, sort_seq=2, body=xml.encode())
    result = engine.export_conversation(sample.config, DIRECT, sample.output)
    messages, manifest, _ = _read_export(sample.output)
    assert result["verified"] and manifest["status"] == "complete"
    assert result["counts"] == {"3:image:available": 1, "49:file:available": 1}
    assert (sample.output / messages[0]["media"][0]["path"]).read_bytes() == png
    assert (sample.output / messages[1]["media"][0]["path"]).read_bytes() == file_payload
    assert "aeskey" not in messages[0]["attachment_meta"]
    assert messages[0]["attachment_meta"]["file_md5"] == image_id
    validation = verify(sample.output, expect_count=2, expect_target="Shared display", deep=True)
    assert validation["verified"] and not validation["issues"]
    assert validation["totals"]["image_available_references"] == 1
    assert validation["totals"]["html_messages"] == 2


def test_no_local_or_external_article_cache_is_read(synthetic_account, monkeypatch):
    sample = synthetic_account
    cached = Mock(return_value={"ok": True, "body": "FORBIDDEN_EXTERNAL_CACHE_BODY"})
    monkeypatch.setattr(engine.link_cache, "load_cached", cached)
    xml = ("<msg><appmsg><title>Synthetic link</title><type>5</type>"
           "<url>https://mp.weixin.qq.com/s/synthetic_test_only</url></appmsg></msg>\x00")
    sample.add(server=7001, kind=49, body=xml.encode())
    engine.export_conversation(sample.config, DIRECT, sample.output)
    assert cached.call_count == 0
    messages, _, _ = _read_export(sample.output)
    assert "Synthetic link" in messages[0]["content"]
    assert "FORBIDDEN_EXTERNAL_CACHE_BODY" not in messages[0]["content"]


@pytest.mark.parametrize("destination", ["account_root", "account_subdirectory", "nonempty", "regular_file"])
def test_unsafe_output_is_rejected_before_any_decryption(synthetic_account, destination):
    sample = synthetic_account
    sample.add()
    if destination == "account_root":
        output = sample.account
        expected = "output_must_be_outside_account"
    elif destination == "account_subdirectory":
        output = sample.account / "must-not-create"
        expected = "output_must_be_outside_account"
    elif destination == "nonempty":
        output = sample.output
        output.mkdir()
        (output / "keep.txt").write_text("Synthetic existing artifact")
        expected = "output_directory_must_be_empty"
    else:
        output = sample.output
        output.write_text("Synthetic existing artifact")
        expected = "output_directory_must_be_empty"
    with pytest.raises(ExportError, match=expected):
        engine.export_conversation(sample.config, DIRECT, output)
    assert not sample.requests
    if destination == "account_subdirectory":
        assert not output.exists()
    if destination == "nonempty":
        assert (output / "keep.txt").read_text() == "Synthetic existing artifact"


def test_unknown_conversation_id_does_not_fall_back_to_matching_display_name(synthetic_account):
    sample = synthetic_account
    sample.add()
    with pytest.raises(ExportError, match="conversation_id_not_found"):
        engine.export_conversation(sample.config, "Shared display", sample.output)
    assert not sample.output.exists()
