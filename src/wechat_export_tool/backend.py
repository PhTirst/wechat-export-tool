"""Pinned WxMori integration; only the explicit capture command touches processes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
from pathlib import Path

from .config import ExportConfig
from .errors import ExportError
from .security import private_directory, private_text_writer, unprotect_bytes


def account_fingerprint(config: ExportConfig) -> str:
    source = os.path.normpath(os.path.abspath(config.account_root / "db_storage"))
    return hashlib.sha256(source.lower().encode("utf-8")).hexdigest()


def account_cache(config: ExportConfig) -> Path:
    return config.cache_dir / "accounts" / account_fingerprint(config)[:24]


def capture_config_path(config: ExportConfig) -> Path:
    return account_cache(config) / "wxmori.json"


def keyring_path(config: ExportConfig) -> Path:
    return account_cache(config) / ".wxmori-keys" / (account_fingerprint(config)[:24] + ".json")


def read_bounded_json(path: Path) -> dict:
    with Path(path).open("rb") as handle:
        data = handle.read((1 << 20) + 1)
    if len(data) > 1 << 20:
        raise ExportError("local_json_too_large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ExportError("local_json_invalid")
    return value


def load_page_keys(config: ExportConfig) -> dict[bytes, bytes]:
    path = keyring_path(config)
    if not path.is_file():
        return {}
    envelope = read_bounded_json(path)
    if envelope.get("format") != "wxmori.dpapi.v1":
        raise ExportError("keyring_format_invalid")
    payload = unprotect_bytes(envelope.get("protected"))
    try:
        material = json.loads(payload)
    finally:
        payload[:] = b"\0" * len(payload)
    if not isinstance(material, dict):
        raise ExportError("keyring_payload_invalid")
    if material.get("version") not in (0, 1) or material.get("account") != account_fingerprint(config):
        raise ExportError("keyring_account_mismatch")
    result = {}
    values = material.get("database_keys")
    if not isinstance(values, dict):
        raise ExportError("keyring_database_keys_invalid")
    try:
        for salt, key in values.items():
            if not isinstance(salt, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", salt):
                raise ExportError("keyring_salt_invalid")
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
                raise ExportError("keyring_key_invalid")
            result[bytes.fromhex(salt)] = bytes.fromhex(key)
    finally:
        values.clear()
        material.clear()
    return result


def _capture_executable(config: ExportConfig) -> Path:
    candidates = [config.capture_exe, shutil.which("wxmori-media"),
                  Path.cwd() / "bin" / "wxmori-media.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise ExportError("capture_executable_missing_run_build_capture")


def prepare_image_probe(config: ExportConfig, conversation_id: str) -> Path:
    """Write only encrypted samples from the explicitly selected chat directory."""
    import base64
    from .image_keys import target_files
    samples = []
    for source in reversed(target_files(config.account_root, conversation_id)):
        with source.open("rb") as handle:
            header = handle.read(15)
            if len(header) != 15 or header[:6] != b"\x07\x08V2\x08\x07":
                continue
            raw_size = struct.unpack_from("<I", header, 6)[0]
            padded = raw_size + 16 - raw_size % 16
            if raw_size < 1 or not 0 < padded <= 2 << 20:
                continue
            ciphertext = handle.read(padded)
        if len(ciphertext) != padded:
            continue
        if sum(len(base64.b64decode(item["ciphertext_base64"])) for item in samples) + padded > 4 << 20:
            continue
        samples.append({"header_hex": header.hex(),
                        "ciphertext_base64": base64.b64encode(ciphertext).decode("ascii")})
        if len(samples) >= 8:
            break
    if not samples:
        raise ExportError("no_local_v2_image_for_selected_conversation")
    output = private_directory(account_cache(config)) / "image-probe.json"
    with private_text_writer(output) as handle:
        json.dump({"version": 1, "samples": samples}, handle)
    return output


def capture_keys(config: ExportConfig, *, timeout: int = 180,
                 pid: int | None = None, images_probe: Path | None = None) -> dict:
    if os.name != "nt":
        raise ExportError("capture_requires_windows")
    if isinstance(timeout, bool) or not 1 <= timeout <= 600:
        raise ExportError("capture_timeout_invalid")
    if pid is not None and (isinstance(pid, bool) or not 0 < pid <= 0xffffffff):
        raise ExportError("capture_pid_invalid")
    if not (config.account_root / "db_storage").is_dir():
        raise ExportError("account_database_directory_missing")
    executable = _capture_executable(config)
    directory = private_directory(account_cache(config))
    capture_config = capture_config_path(config)
    with private_text_writer(capture_config) as handle:
        json.dump({"data_dir": str((config.account_root / "db_storage").absolute()),
                   "output_dir": str(directory / "unused-output"),
                   "timezone": "Asia/Shanghai"}, handle)
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("WXMORI_")}
    environment["WXMORI_STRICT_ACCOUNT_ONLY"] = "1"
    if pid is not None:
        environment["WXMORI_CAPTURE_PID"] = str(pid)
    if images_probe is not None:
        if not Path(images_probe).is_file():
            raise ExportError("image_probe_missing")
        environment["WXMORI_IMAGE_PROBE"] = str(Path(images_probe).resolve())
        environment["WXMORI_IMAGE_KEY_FILE"] = str(directory / "image-candidate.dpapi.json")
    command = [str(executable), "--config", str(capture_config), "key", "capture",
               "--timeout", str(timeout), "--mode", "auto", "--progress", "json"]
    states = {"idle", "waiting_for_wechat", "waiting_for_database", "watching_login",
              "watching_images", "scanning", "completed", "partial", "timeout",
              "stopped", "failed", "unsupported", "waiting_for_databases", "settling",
              "access_denied", "arming_login_capture", "account_mismatch"}
    last = {}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                               errors="replace", env=environment,
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or "state" not in event:
                continue
            clean = {}
            if isinstance(event.get("state"), str) and event["state"] in states:
                clean["state"] = event["state"]
            for key in ("running", "ready_for_login", "image_probe_enabled", "image_key_captured"):
                if type(event.get(key)) is bool:
                    clean[key] = event[key]
            for key in ("verified_databases", "required_databases"):
                value = event.get(key)
                if type(value) is int and 0 <= value <= 0xffffffff:
                    clean[key] = value
            if clean != last:
                print(json.dumps({"capture": clean}, ensure_ascii=False), flush=True)
                last = clean
        returncode = process.wait(timeout=20)
    except KeyboardInterrupt:
        # Let the debugger restore thread registers and detach before exiting.
        process.send_signal(signal.CTRL_BREAK_EVENT)
        process.wait(timeout=30)
        raise
    finally:
        if process.stdout:
            process.stdout.close()
    candidate = directory / "image-candidate.dpapi.json"
    if candidate.is_file() and images_probe is not None:
        # Promotion needs an explicit conversation; normal export validates this
        # candidate against that conversation before accepting any image key.
        last["image_candidate_saved"] = True
    return {"returncode": returncode, "status": last.get("state", "failed"),
            "verified_databases": last.get("verified_databases", 0),
            "required_databases": last.get("required_databases", 0),
            "image_candidate_saved": last.get("image_candidate_saved", False)}
