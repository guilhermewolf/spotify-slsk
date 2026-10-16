"""Small helpers shared by the blueprints."""
from __future__ import annotations

import logging
import os
import time

from flask import current_app, g

from .. import db


def dir_size(path: str) -> int:
    """Sum of file sizes (bytes) under ``path``. Returns 0 for missing dirs.

    Uses os.scandir + recursion (faster than os.walk for many small files).
    Permission errors on individual entries are swallowed — we'd rather
    show an underestimate than 500 the dashboard.
    """
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def relative_to_playlists_root(path: str | None) -> str | None:
    """Return ``path`` relative to the playlists root, or None if outside.

    Used by the track view to build a safe ``?rel=`` query for /file.
    """
    if not path:
        return None
    root = os.path.realpath(os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists"))
    try:
        full = os.path.realpath(path)
        if os.path.commonpath([root, full]) != root:
            return None
        return os.path.relpath(full, root)
    except (ValueError, OSError):
        return None


def human_bytes(n: float) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PiB"


def fmt_ts(epoch: float | None) -> str:
    """Epoch seconds -> local "YYYY-MM-DD HH:MM"."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)) if epoch else "—"


def fmt_interval(seconds: int | None) -> str:
    """3600 -> "1h", 86400 -> "1d", 300 -> "5m"; 0 -> "on demand"."""
    if not seconds:
        return "on demand"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def fmt_age(seconds: float | None) -> str:
    """Seconds -> "3d 4h", "2h 5m", "4m"."""
    seconds = int(seconds or 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def wake_daemon() -> None:
    """Interrupt the daemon's cycle sleep so a change shows up in seconds."""
    cb = current_app.config.get("WAKE_DAEMON")
    if cb is not None:
        try:
            cb()
        except Exception:
            logging.debug("wake_callback raised", exc_info=True)


def health_snapshot() -> dict:
    """Shared by /healthz and the header badge, so they cannot disagree.

    Deliberately cheap: a PRAGMA and a stat() call. No slskd round-trip —
    a health endpoint that depends on a remote service fails when that
    service is slow, which is exactly when you need it to answer.
    """
    try:
        version = g.conn.execute("PRAGMA user_version").fetchone()[0]
        heartbeat_file = os.getenv("HEARTBEAT_FILE", "/tmp/heartbeat")
        heartbeat_age = None
        if os.path.exists(heartbeat_file):
            heartbeat_age = time.time() - os.path.getmtime(heartbeat_file)
        stale = int(os.getenv("HEARTBEAT_STALE_SECONDS", "900"))
        ok = version == db.SCHEMA_VERSION and (
            heartbeat_age is None or heartbeat_age < stale
        )
        if not ok:
            reason = (
                f"schema version {version}, expected {db.SCHEMA_VERSION}"
                if version != db.SCHEMA_VERSION
                else f"no daemon heartbeat for {int(heartbeat_age or 0)}s"
            )
        elif heartbeat_age is None:
            reason = "waiting for the daemon's first heartbeat"
        else:
            reason = f"daemon active {int(heartbeat_age)}s ago"
        return {
            "ok": ok,
            "schema_version": version,
            "heartbeat_age_seconds": heartbeat_age,
            "reason": reason,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "reason": str(e)}
