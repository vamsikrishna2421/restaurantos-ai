"""Online backups (ARCHITECTURE_OPS §11): a consistent copy of the live database with every sandbox row removed.

The SQLite online-backup API copies one consistent snapshot in a single step while writers keep going (WAL). The
copy then loses every row of every ``sbx_*`` tenant (sandbox rows are never backed up, ARCHITECTURE_OPS §13), is
vacuumed so no deleted page survives in the file, and switches to a rollback journal so the backup is one
self-contained file. It is written beside the target and renamed into place, so a crash never leaves a half
backup under the final name. Keeping the last three copies is the maintenance job's concern (P30c).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, delete, or_, select
from sqlalchemy.pool import NullPool

from restaurantos.contracts.common import SANDBOX_PREFIX
from restaurantos.store import tables as t

BACKUP_PREFIX = "ros-"
BACKUP_SUFFIX = ".db"
_TIMEOUT_S = 5.0


def backup_target(dest: Path, now_real_ms: int) -> Path:
    """``dest`` itself when it names a file (it has a suffix and is not a directory); otherwise a directory that
    gets ``ros-YYYYmmddTHHMMSSZ.db`` (UTC, from the store's real clock)."""
    if dest.is_dir() or not dest.suffix:
        stamp = datetime.fromtimestamp(now_real_ms / 1000, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        return dest / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    return dest


def _sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]


def write_backup(source: Path, target: Path) -> Path:
    """Copy ``source`` to ``target`` without sandbox rows (synchronous: run it in a thread)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    for leftover in (partial, *_sidecars(partial)):
        leftover.unlink(missing_ok=True)
    try:
        src = sqlite3.connect(source, timeout=_TIMEOUT_S)
        try:
            dst = sqlite3.connect(partial)
            try:
                src.backup(dst)  # one step: a consistent snapshot of the committed state, WAL included
            finally:
                dst.close()
        finally:
            src.close()
        _strip_sandboxes(partial)
        os.replace(partial, target)
    finally:
        for leftover in (partial, *_sidecars(partial)):
            leftover.unlink(missing_ok=True)
    return target


def _strip_sandboxes(path: Path) -> None:
    tenants = t.tenants
    sandbox_ids = select(tenants.c.id).where(tenants.c.kind == "sandbox").scalar_subquery()
    engine = create_engine("sqlite://", poolclass=NullPool, creator=lambda: sqlite3.connect(path, timeout=_TIMEOUT_S))
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=DELETE")  # one file, no -wal/-shm beside the backup
            conn.commit()
            for table in t.metadata.sorted_tables:
                if "tenant_id" in table.c:
                    conn.execute(delete(table).where(or_(table.c.tenant_id.startswith(SANDBOX_PREFIX, autoescape=True),
                                                         table.c.tenant_id.in_(sandbox_ids))))
            conn.execute(delete(tenants).where(or_(tenants.c.kind == "sandbox",
                                                   tenants.c.id.startswith(SANDBOX_PREFIX, autoescape=True))))
            conn.commit()
            conn.exec_driver_sql("VACUUM")  # rebuild the file so no deleted sandbox page lingers
            conn.commit()
    finally:
        engine.dispose()
