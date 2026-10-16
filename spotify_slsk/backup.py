"""Database backups and daily housekeeping (both daemon tasks).

Backups use SQLite's online backup API, so they are consistent while the
daemon and the web UI keep using the database (WAL mode), and land in
``<data>/backups/playlist_tracks-YYYYMMDD-HHMMSS.db`` beside the database.
Each is written to a ``.partial`` file, switched to a self-contained
rollback journal (no -wal/-shm companions), integrity-checked, and only
then renamed into place. Backups contain the settings table, secrets
included, so the directory is 0700 and each file 0600. Retention keeps the newest BACKUP_RETENTION files
matching that name pattern and touches nothing else in the directory.

To restore: stop the container, copy a backup over
``data/playlist_tracks.db``, delete ``playlist_tracks.db-wal`` and
``-shm`` if present, start it again (README, "Backups").
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from . import config
from .db import prune_history
from .importer import artwork

BACKUP_DIRNAME = "backups"
NAME_RE = re.compile(r"^playlist_tracks-\d{8}-\d{6}\.db$")
# Our own temp files older than this are leftovers of an interrupted run.
STALE_TEMP_SECONDS = 24 * 3600
# Partial downloads in slskd's incomplete/ older than this are reported.
STALE_PARTIAL_DAYS = 7
# VACUUM when this share of the file is free pages (and the file is not tiny).
VACUUM_FREE_RATIO = 0.25
VACUUM_MIN_PAGES = 1000


def db_file(conn: sqlite3.Connection) -> str:
    """Path of the connection's main database file."""
    for _seq, name, path in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            return path
    raise RuntimeError("no main database")


def backup_dir(conn: sqlite3.Connection) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_file(conn))), BACKUP_DIRNAME)


def create_backup(conn: sqlite3.Connection, now: float | None = None) -> str:
    """Write and verify one backup; returns its path. Raises on failure
    (nothing is left behind but a .partial, removed by housekeeping)."""
    directory = backup_dir(conn)
    # Backups hold the settings table - webhook/Subsonic secrets, the login
    # hash, the Flask secret key - so only the container user may read them.
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    dest = os.path.join(directory, f"playlist_tracks-{stamp}.db")
    partial = dest + ".partial"
    # Created 0600 before SQLite writes a byte into it.
    os.close(os.open(partial, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
    os.chmod(partial, 0o600)
    target = sqlite3.connect(partial)
    try:
        conn.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
        rows = [r[0] for r in target.execute("PRAGMA integrity_check").fetchall()]
    finally:
        target.close()
    if rows != ["ok"]:
        os.remove(partial)
        raise RuntimeError("backup failed its integrity check: " + "; ".join(rows[:3]))
    os.replace(partial, dest)
    return dest


def list_backups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Backups, newest first: [{name, path, size, mtime}]."""
    directory = backup_dir(conn)
    try:
        names = [n for n in os.listdir(directory) if NAME_RE.match(n)]
    except OSError:
        return []
    out = []
    for name in sorted(names, reverse=True):
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"name": name, "path": path, "size": st.st_size, "mtime": st.st_mtime})
    return out


def prune_backups(conn: sqlite3.Connection, keep: int) -> int:
    """Delete all but the newest `keep` backups (keep <= 0: delete none)."""
    if keep <= 0:
        return 0
    removed = 0
    for b in list_backups(conn)[keep:]:
        try:
            os.remove(b["path"])
            removed += 1
        except OSError as e:
            logging.warning(f"[backup] Could not remove old backup {b['name']}: {e}")
    return removed


def run_backup(conn: sqlite3.Connection) -> str:
    path = create_backup(conn)
    removed = prune_backups(conn, config.BACKUP_RETENTION)
    size = os.path.getsize(path)
    logging.info(f"[backup] Wrote {path} ({size} bytes); removed {removed} old backup(s)")
    return f"{os.path.basename(path)} ({size // 1024} KiB), removed {removed} old"


# ---- housekeeping ------------------------------------------------------------

def _remove_stale(directory: str, suffixes: tuple[str, ...], older_than: float, now: float) -> int:
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for e in entries:
        try:
            if e.name.endswith(suffixes) and e.is_file(follow_symlinks=False) \
                    and now - e.stat(follow_symlinks=False).st_mtime > older_than:
                os.remove(e.path)
                removed += 1
        except OSError:
            continue
    return removed


def stale_partials(now: float) -> int:
    """Files in slskd's incomplete/ untouched for STALE_PARTIAL_DAYS. Only
    counted: slskd owns that directory and may resume them, so deleting is
    left to the operator."""
    root = os.path.join(config.DOWNLOAD_DIR, "incomplete")
    cutoff = now - STALE_PARTIAL_DAYS * 86400
    count = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                if os.lstat(os.path.join(dirpath, name)).st_mtime < cutoff:
                    count += 1
            except OSError:
                continue
    return count


def maybe_vacuum(
    conn: sqlite3.Connection, heartbeat: Callable[[], None] = lambda: None
) -> str:
    """VACUUM when much of the file is free pages. Returns what happened.

    VACUUM rewrites the whole file and needs an exclusive lock, so it can
    take a while (heartbeat before and after) and can find the database
    busy - the web UI reads it all the time. A failure is logged and left
    for the next daily run; it never fails the task."""
    try:
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if pages < VACUUM_MIN_PAGES or free < pages * VACUUM_FREE_RATIO:
            return "no vacuum needed"
        heartbeat()
        conn.execute("VACUUM")
    except sqlite3.Error as e:
        logging.warning(f"[housekeeping] VACUUM skipped ({e}); retrying on the next run")
        return f"vacuum skipped ({e}); retrying on the next run"
    finally:
        heartbeat()
    return "vacuumed"


def housekeeping(
    conn: sqlite3.Connection,
    now: float | None = None,
    heartbeat: Callable[[], None] = lambda: None,
) -> str:
    """Daily. Never touches library files."""
    now = now if now is not None else time.time()
    prune_history(conn, config.HISTORY_RETENTION_DAYS)
    vacuum = maybe_vacuum(conn, heartbeat)
    # Our own temp files: an artwork negative-cache entry past its TTL, an
    # interrupted artwork write, an interrupted backup.
    artwork_removed = _remove_stale(artwork.cache_dir(), (".fail",), artwork.FAILURE_TTL_SECONDS, now)
    artwork_removed += _remove_stale(artwork.cache_dir(), (".tmp",), STALE_TEMP_SECONDS, now)
    backups_removed = _remove_stale(backup_dir(conn), (".partial",), STALE_TEMP_SECONDS, now)
    partials = stale_partials(now)
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.Error as e:
        logging.warning(f"[housekeeping] PRAGMA optimize skipped: {e}")
    parts = [
        "history trimmed",
        vacuum,
        f"{artwork_removed} expired artwork cache file(s) removed",
    ]
    if backups_removed:
        parts.append(f"{backups_removed} interrupted backup(s) removed")
    if partials:
        parts.append(
            f"{partials} partial download(s) in incomplete/ older than {STALE_PARTIAL_DAYS} days "
            "(report only: delete them by hand if slskd no longer lists them)"
        )
        logging.warning(f"[housekeeping] {parts[-1]}")
    result = "; ".join(parts)
    logging.info(f"[housekeeping] {result}")
    return result
