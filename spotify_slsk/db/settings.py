"""Key-value settings bag: DB value, else env, else default."""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Look up a setting: DB value wins, else env var by the same key,
    else `default`. Always returns a str (or None if no value anywhere).
    """
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row and row[0] is not None:
            return row[0]
    except sqlite3.Error as e:
        logging.warning(f"Could not read setting {key}: {e}")
    env_value = os.getenv(key)
    if env_value is not None:
        return env_value
    return default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Write a DB override for a setting. Pass None to delete the override."""
    if value is None:
        delete_setting(conn, key)
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (key, str(value)),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not write setting {key}: {e}")


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    """Drop the DB override so env/code default takes over again."""
    try:
        with conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    except sqlite3.Error as e:
        logging.error(f"Could not delete setting {key}: {e}")


def list_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """Return all DB-stored settings as a plain dict."""
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {k: v for k, v in rows}
    except sqlite3.Error as e:
        logging.warning(f"Could not list settings: {e}")
        return {}
