"""Blocklist of peer files and whole peers."""
import logging
import sqlite3




# ---- blocklist ------------------------------------------------------------


def add_blocklist(conn, username, filename=None, size=None, reason=None, track_id=None):
    """Blocklist one peer file, or the whole peer when `filename` is None.
    A duplicate (username, filename) is ignored. Returns True if added."""
    if not username:
        return False
    try:
        with conn:
            exists = conn.execute(
                "SELECT 1 FROM blocklist WHERE username = ? AND filename IS ?",
                (username, filename),
            ).fetchone()
            if exists:
                return False
            conn.execute(
                "INSERT INTO blocklist (username, filename, size, reason, track_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, filename, size, reason, track_id),
            )
        logging.info(f"Blocklisted {username}:{filename or '*'} ({reason})")
        return True
    except sqlite3.Error as e:
        logging.error(f"Could not blocklist {username}:{filename}: {e}")
        return False


def list_blocklist(conn, limit: int = 500) -> list:
    """Newest first."""
    try:
        rows = conn.execute(
            "SELECT id, username, filename, size, reason, track_id, created_at "
            "FROM blocklist ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as e:
        logging.warning(f"Could not list blocklist: {e}")
        return []
    keys = ("id", "username", "filename", "size", "reason", "track_id", "created_at")
    return [dict(zip(keys, r)) for r in rows]


def remove_blocklist(conn, entry_id) -> bool:
    try:
        with conn:
            cur = conn.execute("DELETE FROM blocklist WHERE id = ?", (entry_id,))
        return cur.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Could not remove blocklist entry {entry_id}: {e}")
        return False


def load_blocklist(conn):
    """Return a predicate is_blocked(username, filename) over a snapshot of
    the table — one query per search instead of one per file."""
    try:
        rows = conn.execute("SELECT username, filename FROM blocklist").fetchall()
    except sqlite3.Error as e:
        logging.warning(f"Could not load blocklist: {e}")
        rows = []
    banned = {u for u, f in rows if f is None}
    files = {(u, f) for u, f in rows if f is not None}
    return lambda username, filename: username in banned or (username, filename) in files


def is_blocklisted(conn, username, filename) -> bool:
    return load_blocklist(conn)(username, filename)
