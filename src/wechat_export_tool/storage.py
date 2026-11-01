"""Freeze all DB/WAL families together before any decryption or SQL reads."""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from chatlog_keeper.core import _snapshot
from chatlog_keeper.core._private_temp import create_private_temp_dir, cleanup_private_temp_dir

from .errors import ExportError
from .security import secure_file


@contextmanager
def readonly(path: Path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def frozen_families(sources: list[Path]):
    sources = list(dict.fromkeys(Path(p) for p in sources))
    if not sources:
        raise ExportError("no_database_sources")
    for source in sources:
        if source.is_symlink() or not source.is_file():
            raise ExportError("database_source_invalid")
    root = create_private_temp_dir("wechat_export_")
    try:
        snapshots = {source: root / f"frozen-{i:04d}.db" for i, source in enumerate(sources)}
        stable = False
        for _ in range(3):
            before = {source: _snapshot._family_signature(source) for source in sources}
            if any(not signature[0][1] for signature in before.values()):
                raise ExportError("source_database_missing")
            for destination in snapshots.values():
                for suffix in ("", "-wal", "-shm"):
                    destination.with_name(destination.name + suffix).unlink(missing_ok=True)
            try:
                for source, destination in snapshots.items():
                    for suffix in ("", "-wal", "-shm"):
                        part = source.with_name(source.name + suffix)
                        if part.is_symlink():
                            raise ExportError("database_sidecar_is_symlink")
                        if not suffix or part.is_file():
                            dest_part = destination.with_name(destination.name + suffix)
                            shutil.copy2(part, dest_part)
                            secure_file(dest_part)
            except FileNotFoundError:
                continue
            if before == {source: _snapshot._family_signature(source) for source in sources}:
                stable = True
                break
        if not stable:
            raise ExportError("database_families_remained_active")
        yield root, snapshots
    finally:
        cleanup_private_temp_dir(root)
