"""Private file creation and Windows DPAPI, without application-owned key stores."""
from __future__ import annotations

import base64
import ctypes
import os
from pathlib import Path

from chatlog_keeper.core._secrets import (
    _prepare_secret_parent, _windows_acl_is_private, _windows_apply_private_acl,
    private_binary_writer, private_text_writer,
)

from .errors import ExportError


def secure_file(path: Path) -> None:
    path = Path(path)
    if path.is_symlink():
        raise ExportError("secret_path_is_symlink")
    if os.name == "nt":
        if not _windows_apply_private_acl(path, directory=False) or not _windows_acl_is_private(path):
            raise ExportError("private_file_acl_failed")
    else:
        os.chmod(path, 0o600, follow_symlinks=False)


def private_directory(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prepare_secret_parent(path)
    return path


secure_directory = private_directory


def _dpapi(data: bytes, *, decrypt: bool) -> bytearray:
    if os.name != "nt":
        raise ExportError("dpapi_requires_windows")
    if not 0 < len(data) <= 1 << 20:
        raise ExportError("dpapi_payload_size_invalid")
    from ctypes import wintypes as wt

    class Blob(ctypes.Structure):
        _fields_ = [("size", wt.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, dest = Blob(len(data), source_buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        if decrypt:
            fn = crypt.CryptUnprotectData
            fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                           ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(Blob)]
            fn.restype = wt.BOOL
            ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest))
        else:
            fn = crypt.CryptProtectData
            fn.argtypes = [ctypes.POINTER(Blob), wt.LPCWSTR, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(Blob)]
            fn.restype = wt.BOOL
            ok = fn(ctypes.byref(source), "WeChat Export Tool", None, None, None, 1, ctypes.byref(dest))
        if not ok or not dest.data or not 0 < dest.size <= 1 << 20:
            raise ExportError("dpapi_operation_failed")
        return bytearray(ctypes.string_at(dest.data, dest.size))
    finally:
        ctypes.memset(source_buffer, 0, len(data))
        if dest.data:
            ctypes.memset(dest.data, 0, dest.size)
            kernel.LocalFree(ctypes.cast(dest.data, ctypes.c_void_p))


def protect_bytes(payload: bytes) -> str:
    encrypted = _dpapi(bytes(payload), decrypt=False)
    try:
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        encrypted[:] = b"\0" * len(encrypted)


def unprotect_bytes(encoded: str) -> bytearray:
    if not isinstance(encoded, str):
        raise ExportError("dpapi_envelope_invalid")
    try:
        encrypted = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ExportError("dpapi_envelope_invalid") from None
    return _dpapi(encrypted, decrypt=True)
