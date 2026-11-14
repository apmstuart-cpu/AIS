"""Online SQLite backups (consistent while services keep writing) and restore."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from pathlib import Path

from . import db
from .geo import utcnow


def backup(conn, db_path: str, dest_dir: str, keep: int = 14) -> Path:
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    tmp = dest / f".yachtspeed-{stamp}.db"
    target = sqlite3.connect(tmp)
    try:
        conn.backup(target)
        ok = target.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"backup integrity check failed: {ok}")
    finally:
        target.close()
    out = dest / f"yachtspeed-{stamp}.db.gz"
    with open(tmp, "rb") as fi, gzip.open(out, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    tmp.unlink()
    backups = sorted(dest.glob("yachtspeed-*.db.gz"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
    db.kv_set(conn, "last_backup", f"{stamp} {out}")
    return out


def restore(archive: str, db_path: str) -> None:
    """Restore a backup over db_path. Stop the services first."""
    target = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(target) + suffix)
        if p.exists():
            p.rename(str(p) + ".pre-restore")
    opener = gzip.open if archive.endswith(".gz") else open
    with opener(archive, "rb") as fi, open(target, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    c = sqlite3.connect(target)
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    if ok != "ok":
        raise RuntimeError(f"restored database failed integrity check: {ok}")
