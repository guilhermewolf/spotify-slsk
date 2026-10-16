"""The periodic tasks shown on /system/tasks, and when each is due.

A deliberately small registry: a name, a label and an interval. The daemon
owns what each task does (daemon.SCHEDULED) and records every run in the
``tasks`` table (db.tasks); the web UI only reads that table and queues
"Run now" requests, which the daemon picks up at its next checkpoint (the
start of a cycle or between two playlists).
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import config
from .db import get_setting, list_task_rows
from .library.health import SCAN_PENDING_KEY

DAY = 86400
HEALTH_CHECK_INTERVAL = 300


@dataclass(frozen=True)
class Task:
    name: str
    label: str
    # Seconds between runs; 0 = only when requested. Read at call time so
    # a settings change applies without a restart.
    interval: Callable[[], int]
    # Shown instead of the interval for tasks driven by the cycle itself.
    cadence: str | None = None


TASKS: tuple[Task, ...] = (
    Task("spotify_sync", "Spotify sync + downloads", lambda: config.CYCLE_INTERVAL_SECONDS,
         cadence="every cycle"),
    Task("library_scan", "Library health scan",
         lambda: config.LIBRARY_SCAN_INTERVAL_HOURS * 3600),
    Task("quality_backfill", "Quality backfill", lambda: 0, cadence="every cycle"),
    Task("recycle_purge", "Recycle bin purge",
         lambda: DAY if config.RECYCLE_BIN_RETENTION_DAYS > 0 else 0),
    Task("db_integrity", "Database integrity check", lambda: DAY),
    Task("health_checks", "Health checks", lambda: HEALTH_CHECK_INTERVAL),
)
BY_NAME = {t.name: t for t in TASKS}


def is_due(conn: sqlite3.Connection, name: str, now: float | None = None) -> bool:
    """Interval elapsed since the last run (never run counts as due). A
    task with interval 0 is never due on its own."""
    interval = BY_NAME[name].interval()
    if interval <= 0:
        return False
    last = (list_task_rows(conn).get(name) or {}).get("last_run") or 0
    return (now if now is not None else time.time()) - last >= interval


def describe(conn: sqlite3.Connection, now: float | None = None) -> list[dict[str, Any]]:
    """Rows for /system/tasks: registry fields plus the stored last run."""
    now = now if now is not None else time.time()
    stored = list_task_rows(conn)
    out = []
    for task in TASKS:
        row = stored.get(task.name) or {}
        interval = task.interval()
        last = row.get("last_run")
        if task.cadence:
            next_due = None
        elif interval > 0:
            next_due = (last or now) + interval if last else now
        else:
            next_due = None
        out.append({
            "name": task.name,
            "label": task.label,
            "interval": interval,
            "cadence": task.cadence or ("on demand" if interval <= 0 else None),
            "last_run": last,
            "last_duration": row.get("last_duration"),
            "last_result": row.get("last_result"),
            "last_ok": row.get("last_ok"),
            "queued": row.get("queued_at") is not None or (
                task.name == "library_scan"
                and get_setting(conn, SCAN_PENDING_KEY, "0") == "1"
            ),
            "next_due": next_due,
        })
    return out
