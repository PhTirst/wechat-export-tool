"""Prepare pinned redistributable Windows runtimes; never read account data.

This is a developer/build-time command. The resulting portable application does
not need Python, Go, Node.js or npm installed on the recipient's computer.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import ssl
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = "24.22.2"
NODE_ARCHIVE = f"node-v{NODE_VERSION}-win-x64.zip"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ARCHIVE}"
NODE_SHA256 = "ca1bff5d8ead13c9f2ea404a176db46bd430704d5b5113734049fa365e7799e2"
SILK_VERSION = "3.7.1"
SILK_URL = f"https://registry.npmjs.org/silk-wasm/-/silk-wasm-{SILK_VERSION}.tgz"
SILK_SHA512 = "mXPwLRtZxrYV3TZx41jMAeKc80wvmyrcXIcs8HctFxK15Ahz2OJQENYhNgEPeCEOdI6Mbx1NxQsqxzwc3DKerw=="
SILK_SOURCE_COMMIT = "2793875764ad98245c177ac496eb5b7100a71038"
SILK_SOURCE_BASE = f"https://raw.githubusercontent.com/idranme/silk-wasm/{SILK_SOURCE_COMMIT}/binding/libSilkCodec/silk/"
SILK_CODEC_LICENSE_SHA256 = "2605f550752714dccc2b9f38c5951582937b1f1e957bc0d0b51bad5a0ac5f6b6"
SILK_SDK_HEADER = "src/SILK_SDK_SRC_ARM_v1.0.9/interface/SKP_Silk_SDK_API.h"
SILK_SDK_HEADER_SHA256 = "1a248d7c2fa783ee21ab25f7a7e9a23cedd38ace3251f9b39f85a327daaee557"
OPENSSL_LICENSE_SHA256 = "7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a"
MAX_DOWNLOAD = 200 << 20


def digest(path: Path, name: str = "sha256") -> str:
    checksum = hashlib.new(name)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            checksum.update(block)
    return checksum.hexdigest()


def python_openssl_version() -> str:
    # OpenSSL 3 uses a changed packed numeric layout; some Python builds expose
    # its fix number in a different VERSION_INFO slot. The linked runtime's
    # canonical version text avoids guessing how those slots were decoded.
    match = re.match(r"^OpenSSL (\d+\.\d+\.\d+)(?:\s|$)", ssl.OPENSSL_VERSION)
    if not match:
        raise ValueError("python_ssl_runtime_requires_license_review")
    return match[1]


def download(url: str, target: Path, *, algorithm: str, expected: str) -> Path:
    """Only publish a download after checking its pinned digest."""
    if target.is_symlink():
        raise ValueError("runtime_download_is_symlink")
    if target.exists():
        if digest(target, algorithm) != expected:
            raise ValueError("runtime_cached_download_digest_mismatch")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "wechat-export-tool-build"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("xb") as handle:
        if response.geturl().split(":", 1)[0] != "https":
            raise ValueError("runtime_insecure_download_redirect")
        count = 0
        while block := response.read(1 << 20):
            count += len(block)
            if count > MAX_DOWNLOAD:
                raise ValueError("runtime_download_too_large")
            handle.write(block)
    if digest(partial, algorithm) != expected:
        raise ValueError("runtime_download_digest_mismatch")
    partial.replace(target)
    return target


def write_selected(output: Path, name: str, data: bytes, selected: set[str]) -> None:
    path = output / name
    if path.resolve().is_relative_to(output.resolve()) is False or path.is_symlink():
        raise ValueError("runtime_output_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    selected.add(name)


def modules(source: Path) -> list[dict]:
    """Get modules actually linked into the capture command, excluding test deps."""
    environment = dict(os.environ, GOOS="windows", GOARCH="amd64")
    process = subprocess.run(["go", "list", "-deps", "-json", "./cmd/wxmori"], cwd=source, env=environment,
        text=True, encoding="utf-8", capture_output=True, check=False, timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode:
        raise RuntimeError("capture_module_inventory_failed")
    result, offset = [], 0
    decoder = json.JSONDecoder()
    while offset < len(process.stdout):
        while offset < len(process.stdout) and process.stdout[offset].isspace():
            offset += 1
        if offset == len(process.stdout):
            break
        item, offset = decoder.raw_decode(process.stdout, offset)
        if item.get("Module"):
            result.append(item["Module"])
    return list({module["Path"]: module for module in result}.values())


def capture_licenses(source: Path, output: Path, selected: set[str]) -> dict:
    if not (source / "go.mod").is_file():
        raise ValueError("capture_source_missing_build_capture_first")
    components = []
    for module in modules(source):
        module_path = module["Path"]
        version = module.get("Version", "055f6992c0a78164b720d47ef48830825fc0b92a+media-patch")
        location = module.get("Replace", module).get("Dir")
        if not location:
            raise ValueError("capture_module_source_directory_missing")
        directory = Path(location)
        if not directory.is_dir():
            raise ValueError("capture_module_source_directory_missing")
        # Include root and nested notices. Binary module files and source code
        # are deliberately excluded from this license-only copy operation.
        notices = sorted(path for path in directory.rglob("*")
            if path.is_file() and not path.is_symlink() and
            re.match(r"(?i)^(?:licen[cs]e|copying|notice|copyright)(?:$|[._-])", path.name))
        if not notices:
            raise ValueError("capture_dependency_license_missing:" + module_path)
        token = re.sub(r"[^A-Za-z0-9._-]", "_", module_path)
        license_paths = []
        for notice in notices:
            relative = notice.relative_to(directory).as_posix()
            destination = f"capture-licenses/{token}/{relative}"
            payload = notice.read_bytes()
            if len(payload) > 4 << 20 or b"\0" in payload:
                raise ValueError("capture_license_not_text")
            write_selected(output, destination, payload, selected)
            license_paths.append(destination)
        components.append({"name": module_path, "version": version, "licenses": license_paths})
    # Go's standard library is statically included in the capture binary.
    process = subprocess.run(["go", "env", "GOROOT"], cwd=source, capture_output=True,
        text=True, encoding="utf-8", check=False, timeout=30)
    if process.returncode:
        raise RuntimeError("go_runtime_license_inventory_failed")
    go_license = Path(process.stdout.strip()) / "LICENSE"
    if not go_license.is_file():
        raise ValueError("go_runtime_license_missing")
    write_selected(output, "capture-licenses/Go-LICENSE.txt", go_license.read_bytes(), selected)
    vendor = go_license.parent / "src/vendor"
    if vendor.is_dir():
        for notice in sorted(vendor.rglob("*")):
            if notice.is_file() and re.match(r"(?i)^(?:licen[cs]e|notice|copyright)(?:$|[._-])", notice.name):
                write_selected(output, "capture-licenses/Go-vendor/" + notice.relative_to(vendor).as_posix(),
                               notice.read_bytes(), selected)
    return {"name": "WxMori capture and Go modules", "source":
        "https://github.com/axiomzero-qed/WxMori/tree/055f6992c0a78164b720d47ef48830825fc0b92a",
        "modules": components, "go_license": "capture-licenses/Go-LICENSE.txt"}


def prepare(output: Path, capture_source: Path) -> dict:
    output = output.resolve()
    if not output.is_relative_to(ROOT / ".build"):
        raise ValueError("runtime_output_must_be_inside_project_build_directory")
    output.mkdir(parents=True, exist_ok=True)
    selected: set[str] = set()
    downloads = output / ".downloads"
    node = download(NODE_URL, downloads / NODE_ARCHIVE, algorithm="sha256", expected=NODE_SHA256)
    with zipfile.ZipFile(node) as archive:
        prefix = f"node-v{NODE_VERSION}-win-x64/"
        for source, destination in [("node.exe", "node/node.exe"), ("LICENSE", "node/LICENSE")]:
            write_selected(output, destination, archive.read(prefix + source), selected)
    silk = download(SILK_URL, downloads / f"silk-wasm-{SILK_VERSION}.tgz", algorithm="sha512",
        expected=base64.b64decode(SILK_SHA512).hex())
    with tarfile.open(silk, "r:gz") as archive:
        for name in ("lib/index.cjs", "lib/silk.wasm", "LICENSE", "package.json"):
            member = archive.getmember("package/" + name)
            if not member.isfile() or member.size > 1 << 20:
                raise ValueError("silk_selected_archive_member_invalid")
            with archive.extractfile(member) as handle:
                write_selected(output, "silk/" + name, handle.read(), selected)
    codec_license = download(SILK_SOURCE_BASE + "LICENSE", downloads / "silk-codec-LICENSE",
                             algorithm="sha256", expected=SILK_CODEC_LICENSE_SHA256)
    write_selected(output, "silk/SILK-CODEC-LICENSE.txt", codec_license.read_bytes(), selected)
    sdk_header = download(SILK_SOURCE_BASE + SILK_SDK_HEADER, downloads / "SKP_Silk_SDK_API.h",
                          algorithm="sha256", expected=SILK_SDK_HEADER_SHA256)
    # Preserve the original BSD-style Skype notice from the compiled SDK,
    # separately from the wrapper package's MIT and codec fork's BSD notice.
    sdk_notice = sdk_header.read_bytes().split(b"*/", 1)[0] + b"*/\n"
    if b"Skype Limited" not in sdk_notice:
        raise ValueError("silk_sdk_notice_missing")
    write_selected(output, "silk/SKYPE-SILK-SDK-LICENSE.txt", sdk_notice, selected)
    openssl_version = python_openssl_version()
    openssl_source = f"https://github.com/openssl/openssl/tree/openssl-{openssl_version}"
    openssl_license_url = f"https://raw.githubusercontent.com/openssl/openssl/openssl-{openssl_version}/LICENSE.txt"
    openssl_license = download(openssl_license_url, downloads / f"openssl-{openssl_version}-LICENSE.txt",
                               algorithm="sha256", expected=OPENSSL_LICENSE_SHA256)
    write_selected(output, "OpenSSL/LICENSE.txt", openssl_license.read_bytes(), selected)
    attribution = (f"OpenSSL {openssl_version}, supplied with the bundled CPython standard library.\n"
        "OpenSSL Project Authors; https://www.openssl.org/\n"
        "Licensed under the Apache License 2.0; see LICENSE.txt in this directory.\n"
        f"Corresponding upstream source and license: {openssl_source}\n\n"
        "This attribution file was prepared by WeChat Export Tool. It is not an upstream\n"
        "NOTICE file; the verified OpenSSL 3.0.16 tag contains no separate NOTICE file.\n"
        "The OpenSSL included inside Node.js is covered separately by node/LICENSE.\n")
    write_selected(output, "OpenSSL/NOTICE.txt", attribution.encode("utf-8"), selected)
    capture = capture_licenses(capture_source.resolve(), output, selected)
    manifest = {"schema_version": 1, "platform": "win-x64", "ffmpeg_bundled": False,
        "components": [{"name": "Node.js", "version": NODE_VERSION, "url": NODE_URL,
            "sha256": NODE_SHA256, "license": "node/LICENSE",
            "checksum_source": f"https://nodejs.org/dist/v{NODE_VERSION}/SHASUMS256.txt",
            "source_url": f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}.tar.xz"},
            {"name": "silk-wasm", "version": SILK_VERSION, "url": SILK_URL,
             "integrity": "sha512-" + SILK_SHA512, "sha256": digest(silk), "license": "silk/LICENSE",
             "source_url": f"https://github.com/idranme/silk-wasm/tree/{SILK_SOURCE_COMMIT}",
             "license_notices": ["silk/LICENSE", "silk/SILK-CODEC-LICENSE.txt", "silk/SKYPE-SILK-SDK-LICENSE.txt"],
             "codec_license_url": SILK_SOURCE_BASE + "LICENSE",
             "sdk_notice_source": SILK_SOURCE_BASE + SILK_SDK_HEADER},
            {"name": "OpenSSL supplied with CPython", "version": openssl_version,
             "license": "OpenSSL/LICENSE.txt", "attribution": "OpenSSL/NOTICE.txt",
             "source_url": openssl_source, "license_url": openssl_license_url,
             "license_sha256": OPENSSL_LICENSE_SHA256, "notice_only": True}, capture],
        "selected_files": sorted(selected),
        "file_sha256": {name: digest(output / name) for name in sorted(selected)}}
    (output / "runtime-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                                 encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".build/runtimes")
    parser.add_argument("--capture-source", type=Path, default=ROOT / ".build/wxmori")
    arguments = parser.parse_args()
    try:
        manifest = prepare(arguments.output, arguments.capture_source)
    except Exception as exc:
        print(json.dumps({"prepared": False, "error": type(exc).__name__}))
        return 1
    print(json.dumps({"prepared": True, "platform": manifest["platform"],
                      "node": NODE_VERSION, "silk": SILK_VERSION,
                      "selected_files": len(manifest["selected_files"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
