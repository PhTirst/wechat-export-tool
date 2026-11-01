"""Exact-MD5 emoji metadata and independently validated offline AES payloads.

The caller owns database snapshot/decryption and supplies a read-only SQLite
connection. This module never opens an account database or a key cache.
"""
from __future__ import annotations

import hashlib
import io
import re
import sqlite3

_MD5 = re.compile(r"^[0-9a-fA-F]{32}$")
MAX_EMOJI_BYTES = 200 * 1024 * 1024


def aes_keys(value):
    """Normalize a per-emoji database/XML key without deriving or guessing it."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        values = []
        # SQLite BLOB often contains the same ASCII hexadecimal text as a TEXT
        # column. Keep its independently valid raw interpretation as well.
        if len(raw) in (32, 48, 64) and re.fullmatch(rb"[0-9a-fA-F]+", raw):
            values.append(bytes.fromhex(raw.decode("ascii")))
        if len(raw) in (16, 24, 32) and raw not in values:
            values.append(raw)
        return values
    if not isinstance(value, str):
        return []
    values = []
    try:
        decoded = bytes.fromhex(value)
        if len(decoded) in (16, 24, 32):
            values.append(decoded)
    except ValueError:
        pass
    encoded = value.encode("utf-8")
    if len(encoded) in (16, 24, 32) and encoded not in values:
        values.append(encoded)
    return values


def load_emoji_metadata(connection: sqlite3.Connection, md5_values) -> dict[str, dict]:
    """Read only requested exact MD5 rows from an already verified database.

    Values: ``aes_keys`` (bytes, memory-only), ``extern_md5s`` (explicit DB
    aliases), and ``packages`` (relative path + bounded offsets and sizes).
    There is no URL access, caption lookup, fuzzy filename search, or database
    scan. Missing tables/columns are treated as unsupported metadata.
    """
    wanted = sorted({value.lower() for value in md5_values if isinstance(value, str) and _MD5.fullmatch(value)})
    result = {value: {"aes_keys": [], "extern_md5s": [], "packages": []} for value in wanted}
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(kNonStoreEmoticonTable)")}
        fields = [name for name in ("aes_key", "extern_md5") if name in columns]
        if "md5" in columns and fields:
            query = "SELECT " + ",".join('"' + field + '"' for field in fields) + " FROM kNonStoreEmoticonTable WHERE md5=? COLLATE NOCASE"
            for digest in wanted:
                for row in connection.execute(query, (digest,)):
                    values = dict(zip(fields, row))
                    for key in aes_keys(values.get("aes_key")):
                        if key not in result[digest]["aes_keys"]:
                            result[digest]["aes_keys"].append(key)
                    alias = values.get("extern_md5")
                    if isinstance(alias, str) and _MD5.fullmatch(alias) and alias.lower() != digest and alias.lower() not in result[digest]["extern_md5s"]:
                        result[digest]["extern_md5s"].append(alias.lower())
    except sqlite3.Error:
        pass
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(kStoreEmoticonFilesTable)")}
        fields = ("package_id_", "emoticon_size_", "emoticon_offset_", "thumb_size_", "thumb_offset_")
        if {"md5_", *fields} <= columns:
            query = "SELECT " + ",".join('"' + field + '"' for field in fields) + " FROM kStoreEmoticonFilesTable WHERE md5_=? COLLATE NOCASE"
            for digest in wanted:
                for row in connection.execute(query, (digest,)):
                    values = dict(zip(fields, row))
                    package_id = values.get("package_id_")
                    if not isinstance(package_id, str) or not package_id or len(package_id) > 4096:
                        continue
                    package_hash = hashlib.md5(package_id.encode("utf-8")).hexdigest()
                    for prefix, thumbnail in (("emoticon", False), ("thumb", True)):
                        try:
                            offset, size = int(values[prefix + "_offset_"]), int(values[prefix + "_size_"])
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if offset < 0 or not 0 < size <= MAX_EMOJI_BYTES or offset > 2**63 - size:
                            continue
                        record = {"path": f"business/emoticon/PersistStore/{package_hash[:2]}/{package_hash}",
                                  "offset": offset, "size": size, "thumbnail": thumbnail}
                        if record not in result[digest]["packages"]:
                            result[digest]["packages"].append(record)
    except sqlite3.Error:
        pass
    return result


def _complete_pixels(data: bytes, validate) -> bool:
    """Independently require every image frame when no GCM tag is present."""
    if not validate(data):
        return False
    try:
        from PIL import Image, ImageSequence
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            for frame in ImageSequence.Iterator(image):
                frame.load()
        return True
    except Exception:
        return False


def decode_emoji_aes(raw: bytes, keys, validate, expected_md5s=None) -> bytes | None:
    """Try documented per-emoji AES forms, requiring full image validation.

    Normal block-aligned ECB/CBC and authenticated GCM retain existing support.
    GCM without a stored tag and block-aligned prefixes with a short trailing
    metadata remainder additionally require the *complete* decoded image MD5
    to equal an exact message MD5 or explicit database extern_md5 supplied by
    the caller. All image frames must load. No filename or guessed hash is used.
    """
    if not raw or len(raw) > MAX_EMOJI_BYTES:
        return None
    from Crypto.Cipher import AES
    normalized = []
    for value in keys:
        for key in aes_keys(value):
            if key not in normalized:
                normalized.append(key)
    if isinstance(expected_md5s, str):
        expected_md5s = [expected_md5s]
    expected = {value.lower() for value in expected_md5s or ()
                if isinstance(value, str) and _MD5.fullmatch(value)}

    def accepted(data, *, checksum=False, padded=False):
        variants = [data]
        padding = data[-1] if data else 0
        if padded and 0 < padding <= 16 and data.endswith(bytes([padding]) * padding):
            variants.insert(0, data[:-padding])
        for content in variants:
            if checksum:
                if (not expected or hashlib.md5(content).hexdigest() not in expected or
                        not _complete_pixels(content, validate)):
                    continue
            elif not validate(content):
                continue
            return content
        return None

    def block_candidates(key, aligned):
        yield AES.new(key, AES.MODE_ECB).decrypt(aligned)
        yield AES.new(key, AES.MODE_CBC, iv=b"\0" * 16).decrypt(aligned)
        if len(aligned) > 16:
            yield AES.new(key, AES.MODE_CBC, iv=aligned[:16]).decrypt(aligned[16:])

    for key in normalized:
        if len(raw) >= 16 and len(raw) % 16 == 0:
            for data in block_candidates(key, raw):
                content = accepted(data, padded=True)
                if content is not None:
                    return content
        # This is exactly the observed aligned-prefix convention, removing at
        # most 15 trailing metadata bytes. No arbitrary offset/block chopping.
        remainder = len(raw) % 16
        if expected and remainder and len(raw) >= 16:
            for data in block_candidates(key, raw[:-remainder]):
                content = accepted(data, checksum=True, padded=True)
                if content is not None:
                    return content
        if len(raw) > 28:
            # N/C/T permutations; all are accepted only after 128-bit tag
            # verification. These cover both prefixed and suffixed nonces.
            authenticated_layouts = (
                (raw[:12], raw[12:-16], raw[-16:]),     # N C T
                (raw[:12], raw[28:], raw[12:28]),      # N T C
                (raw[-12:], raw[:-28], raw[-28:-12]), # C T N
                (raw[-12:], raw[16:-12], raw[:16]),   # T C N
                (raw[-28:-16], raw[:-28], raw[-16:]), # C N T
                (raw[16:28], raw[28:], raw[:16]),     # T N C
            )
            for nonce, payload, tag in authenticated_layouts:
                try:
                    data = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(payload, tag)
                except ValueError:
                    continue
                content = accepted(data)
                if content is not None:
                    return content
        if expected and len(raw) > 12:
            # The known tagless envelope stores its 12-byte nonce at one end.
            # No padding removal, magic-byte shortcut, or metadata trimming.
            for nonce, payload in ((raw[:12], raw[12:]), (raw[-12:], raw[:-12])):
                data = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt(payload)
                content = accepted(data, checksum=True)
                if content is not None:
                    return content
    return None
