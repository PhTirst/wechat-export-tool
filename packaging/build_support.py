"""Explicit, private-data-free inputs and inventory for the Windows bundle."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


PROJECT = Path(__file__).resolve().parents[1]
DISTRIBUTIONS = ("wechat-export-tool", "chatlog-keeper", "Pillow", "pycryptodome", "numpy", "zstandard")
METADATA_FILES = ("METADATA", "WHEEL", "entry_points.txt", "top_level.txt")
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".key", ".dat"}
FORBIDDEN_PARTS = {".git", ".private", ".tools", "exports", ".downloads", "__pycache__"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def project_version() -> str:
    version = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.-]+)?", version):
        raise ValueError("invalid_project_version")
    return version


def prepare_metadata(destination: Path) -> list[tuple[str, str]]:
    """Preserve runtime metadata and licenses, never editable direct_url/RECORD.

    copy_metadata() copies whole dist-info directories, including local editable
    install paths in direct_url.json. Only the known nonlocal metadata files are
    rebuilt here; dependency package code is collected separately by Analysis.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    result = []
    for name in DISTRIBUTIONS:
        distribution = metadata.distribution(name)
        directory = re.sub(r"[-.]", "_", distribution.metadata["Name"]) + "-" + distribution.version + ".dist-info"
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.dist-info", directory):
            raise ValueError("invalid_distribution_metadata")
        output = destination / directory
        output.mkdir(parents=True, exist_ok=True)
        for filename in METADATA_FILES:
            content = distribution.read_text(filename)
            if content is None:
                continue
            if name == "wechat-export-tool" and filename == "METADATA":
                # Source builds must not silently retain a previous editable
                # installation's version after the project version changes.
                if distribution.version != project_version():
                    raise ValueError("installed_project_version_is_stale")
            path = output / filename
            path.write_text(content, encoding="utf-8", newline="\n")
            result.append((str(path), directory))
    return result


def publishable_datas(entries):
    """Drop build-machine metadata also injected by PyInstaller's own hooks."""
    private_metadata = {"direct_url.json", "record", "installer", "requested"}
    result = []
    for entry in entries:
        destination = PurePosixPath(str(entry[0]).replace("\\", "/"))
        if (any(part.lower().endswith((".dist-info", ".egg-info")) for part in destination.parts)
                and destination.name.lower() in private_metadata):
            continue
        result.append(entry)
    return result


def runtime_inputs(root: Path) -> tuple[dict, list[tuple[Path, str]]]:
    """Return only the hash-pinned files selected by runtime preparation."""
    root = Path(root).resolve(strict=True)
    manifest = root / "runtime-manifest.json"
    if manifest.is_symlink() or manifest.stat().st_size > 1 << 20:
        raise ValueError("runtime_manifest_invalid")
    values = json.loads(manifest.read_text(encoding="utf-8"))
    selected = values.get("selected_files")
    hashes = values.get("file_sha256")
    if values.get("schema_version") != 1 or values.get("platform") != "win-x64":
        raise ValueError("runtime_manifest_platform_invalid")
    if not isinstance(selected, list) or not selected or len(selected) > 1000 or not all(isinstance(item, str) for item in selected) or not isinstance(hashes, dict):
        raise ValueError("runtime_manifest_files_invalid")
    if len(set(selected)) != len(selected):
        raise ValueError("runtime_manifest_duplicate_files")
    required = {"node/node.exe", "node/LICENSE", "silk/LICENSE"}
    if not required.issubset(selected) or not ({"silk/lib/index.cjs", "silk/index.cjs"} & set(selected)):
        raise ValueError("runtime_manifest_required_files_missing")
    if not ({"silk/lib/silk.wasm", "silk/silk.wasm"} & set(selected)):
        raise ValueError("runtime_manifest_required_files_missing")
    files = []
    for relative in selected:
        if not isinstance(relative, str) or "\\" in relative or ":" in relative:
            raise ValueError("runtime_manifest_path_invalid")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "." in path.parts or not path.parts:
            raise ValueError("runtime_manifest_path_invalid")
        if path.parts[0] not in {"node", "silk", "capture-licenses", "ffmpeg", "openssl", "OpenSSL"}:
            raise ValueError("runtime_manifest_component_invalid")
        if any(part in FORBIDDEN_PARTS for part in path.parts) or path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.endswith((".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm")):
            raise ValueError("runtime_manifest_private_file")
        source = root.joinpath(*path.parts)
        if source.is_symlink() or not source.is_file():
            raise ValueError("runtime_manifest_source_invalid")
        source.resolve(strict=True).relative_to(root)
        expected = hashes.get(relative)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256(source) != expected:
            raise ValueError("runtime_manifest_hash_mismatch")
        files.append((source, relative))
    return values, files


def _copy(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file() or destination.exists():
        raise ValueError("bundle_copy_source_or_destination_invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _license_files(distribution_name: str):
    distribution = metadata.distribution(distribution_name)
    for item in distribution.files or ():
        path = PurePosixPath(str(item).replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or ":" in str(path):
            continue
        # License files can be in dist-info/licenses or in a package itself.
        if any(part in {"tests", "test", "testing"} for part in path.parts):
            continue
        if not any(part.lower().startswith(("license", "licence", "copying", "notice")) for part in path.parts):
            continue
        source = Path(distribution.locate_file(item))
        if source.is_file() and not source.is_symlink() and source.stat().st_size <= 8 << 20:
            yield source, path


def stage_bundle(bundle: Path, runtime_root: Path, capture: Path) -> dict:
    bundle = Path(bundle).resolve(strict=True)
    for executable in ("wechat-export.exe", "WeChatExportTool.exe"):
        if not (bundle / executable).is_file():
            raise ValueError("bundle_executable_missing")
    values, selected = runtime_inputs(runtime_root)
    for source, relative in selected:
        _copy(source, bundle / "runtime" / relative)
    _copy(Path(runtime_root).resolve() / "runtime-manifest.json", bundle / "runtime/runtime-manifest.json")
    capture = Path(capture).resolve(strict=True)
    if capture.suffix.lower() != ".exe":
        raise ValueError("capture_executable_invalid")
    _copy(capture, bundle / "runtime/capture/wxmori-media.exe")
    for name in ("LICENSE", "README.md", "THIRD_PARTY_NOTICES.md"):
        _copy(PROJECT / name, bundle / name)
    for name in ("DEPENDENCIES.md", "JSON_FORMAT.md", "WINDOWS_RELEASE.md", "CLI_AUTOMATION.md",
                 "WINDOWS_RUNTIME_LICENSES.md", "FFMPEG_MINIMAL_BUILD.md"):
        _copy(PROJECT / "docs" / name, bundle / "docs" / name)
    # This is a source allowlist, never a recursive workspace copy.
    for name in ("README.md", "WxMori-MIT.txt", "chatlog-keeper-MIT.txt", "silk-wasm-MIT.txt",
                 "wechatauto-replica-Apache-2.0.txt", "Nodejs-v24.22.2.txt", "SILK-codec-BSD.txt",
                 "Skype-SILK-SDK-BSD.txt"):
        _copy(PROJECT / "licenses" / name, bundle / "licenses" / name)
    licenses = bundle / "third-party-licenses"
    _copy(Path(sys.base_prefix) / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    tcl_license = PROJECT / "packaging/licenses/Tcl-license.terms"
    _copy(tcl_license, licenses / "Tcl-license.terms")
    tk_licenses = sorted((Path(sys.base_prefix) / "tcl").glob("tk*/license.terms"))
    if not tk_licenses:
        raise ValueError("tk_license_missing")
    _copy(tk_licenses[-1], licenses / "Tk-license.terms")
    for name in (*DISTRIBUTIONS, "pyinstaller"):
        for source, relative in _license_files(name):
            normalized = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            # Keep package-relative paths so multiple notices cannot collide.
            destination = licenses / normalized / Path(*relative.parts)
            if not destination.exists():
                _copy(source, destination)
    provenance = {"schema_version": 1, "project": "wechat-export-tool", "version": project_version(),
        "platform": "windows-x64", "python": sys.version.split()[0],
        "pyinstaller": metadata.version("pyinstaller"), "capture_sha256": sha256(capture),
        "runtime_components": values.get("components", []),
        "distributions": {name: metadata.version(name) for name in DISTRIBUTIONS}}
    (bundle / "build-info.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return provenance


def inventory_bundle(bundle: Path) -> dict:
    bundle = Path(bundle).resolve(strict=True)
    entries = []
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle)
        if path.is_symlink() or any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise ValueError("bundle_contains_private_path")
        if not path.is_file() or relative.as_posix() == "bundle-manifest.json":
            continue
        if (any(part.lower().endswith((".dist-info", ".egg-info")) for part in relative.parts)
                and path.name.lower() in {"direct_url.json", "record", "installer", "requested"}):
            raise ValueError("bundle_contains_private_metadata")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name in {"direct_url.json", "config.local.json"} or path.name.endswith((".dpapi.json", ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm")):
            raise ValueError("bundle_contains_private_file")
        entries.append({"path": relative.as_posix(), "size": path.stat().st_size, "sha256": sha256(path)})
    if not entries:
        raise ValueError("bundle_empty")
    result = {"schema_version": 1, "version": project_version(), "files": entries}
    (bundle / "bundle-manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def finalize(bundle: Path, destination: Path) -> Path:
    bundle = Path(bundle).resolve(strict=True)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    inventory_bundle(bundle)
    artifact = destination / f"WeChatExportTool-v{project_version()}-windows-x64.zip"
    if artifact.exists():
        raise ValueError("release_archive_already_exists")
    with zipfile.ZipFile(artifact, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, "WeChatExportTool/" + path.relative_to(bundle).as_posix())
    (destination / "SHA256SUMS.txt").write_text(f"{sha256(artifact)}  {artifact.name}\n", encoding="ascii")
    return artifact


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("validate-runtime")
    prepare.add_argument("--runtime-root", type=Path, required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("--bundle", type=Path, required=True)
    stage.add_argument("--runtime-root", type=Path, required=True)
    stage.add_argument("--capture", type=Path, required=True)
    finish = sub.add_parser("finalize")
    finish.add_argument("--bundle", type=Path, required=True)
    finish.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "validate-runtime":
        _, files = runtime_inputs(args.runtime_root)
        print(json.dumps({"runtime_files": len(files), "verified": True}))
    elif args.command == "stage":
        print(json.dumps(stage_bundle(args.bundle, args.runtime_root, args.capture)))
    else:
        path = finalize(args.bundle, args.destination)
        print(json.dumps({"artifact": str(path), "sha256": sha256(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
