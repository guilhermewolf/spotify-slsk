"""tasks (schema v9): last run of each scheduled task, and "Run now" requests.

One row per task name. ``queued_at`` is the run-now request: setting it
when it is already set is a no-op, so repeated clicks collapse into one run
(Lidarr's command-queue dedupe, without the command framework).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any


def record_task_run(
    conn: sqlite3.Connection,
    name: str,
    *,
    started: float,
    duration: float,
    result: str,
    ok: bool = True,
) -> None:
    try:
        with conn:
            conn.execute(
                "INSERT INTO tasks (name, last_run, last_duration, last_result, last_ok) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "last_run = excluded.last_run, last_duration = excluded.last_duration, "
                "last_result = excluded.last_result, last_ok = excluded.last_ok",
                (name, started, duration, result[:500], int(ok)),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not record task run {name}: {e}")


def queue_task(conn: sqlite3.Connection, name: str) -> bool:
    """Request a run. False when one is already queued (deduplicated)."""
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO tasks (name, queued_at) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET queued_at = excluded.queued_at "
                "WHERE tasks.queued_at IS NULL",
                (name, time.time()),
            )
        return cur.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Could not queue task {name}: {e}")
        return False


def pop_queued_tasks(conn: sqlite3.Connection, names: list[str] | tuple[str, ...]) -> set[str]:
    """Clear and return the queued requests among `names`."""
    if not names:
        return set()
    marks = ",".join("?" * len(names))
    try:
        with conn:
            rows = conn.execute(
                f"SELECT name FROM tasks WHERE queued_at IS NOT NULL AND name IN ({marks})",
                tuple(names),
            ).fetchall()
            conn.execute(
                f"UPDATE tasks SET queued_at = NULL WHERE name IN ({marks})", tuple(names)
            )
        return {r[0] for r in rows}
    except sqlite3.Error as e:
        logging.error(f"Could not read queued tasks: {e}")
        return set()


def list_task_rows(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT name, last_run, last_duration, last_result, last_ok, queued_at FROM tasks"
        ).fetchall()
    except sqlite3.Error as e:
        logging.warning(f"Could not list tasks: {e}")
        return {}
    return {
        r[0]: {
            "last_run": r[1], "last_duration": r[2], "last_result": r[3],
            "last_ok": None if r[4] is None else bool(r[4]), "queued_at": r[5],
        }
        for r in rows
    }
