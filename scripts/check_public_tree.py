"""Fail if git's publish set includes private artifacts or machine-specific data."""
from __future__ import annotations
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOW = {"src", "tests", "docs", "licenses", "patches", "scripts", "packaging", ".github", "examples"}
TOP = {".gitignore", ".gitattributes", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml", "package.json", "package-lock.json", "MANIFEST.in", "requirements-build.txt"}
FORBIDDEN = re.compile(r"(?i)(?:[A-Z]:[\\/]Users[\\/](?!Public(?:[\\/]|$))[^\\/\s]+|wxid_[a-z0-9]{8,}|(?:sk-|gh[pousr]_)[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")

def main():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    failures = []
    for raw in filter(None, paths):
        path = Path(raw)
        if (len(path.parts) == 1 and raw not in TOP) or (len(path.parts) > 1 and path.parts[0] not in ALLOW):
            failures.append((raw, "outside_publish_allowlist")); continue
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".key", ".exe", ".wasm", ".wav", ".mp4", ".dat"}:
            failures.append((raw, "private_or_binary_artifact")); continue
        content = (ROOT / path).read_bytes()
        if b"\0" in content:
            failures.append((raw, "unexpected_binary")); continue
        if FORBIDDEN.search(content.decode("utf-8")):
            failures.append((raw, "possible_private_value"))
    if failures:
        for path, reason in failures: print(f"FAIL {path}: {reason}")
        return 1
    print(f"Public tree verified: {sum(bool(p) for p in paths)} source/documentation files; no private artifacts.")
    return 0

if __name__ == "__main__": raise SystemExit(main())
