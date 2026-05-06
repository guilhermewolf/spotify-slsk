"""In-process runtime state shared between the daemon thread and the web UI.

Two pieces of state live here:

1. A thread-safe **activity tracker** — what the daemon is doing right now
   (idle, syncing playlist X, downloading track Y, …). Set by the daemon,
   read by the web UI on every request. Cheap; just a dict + lock.

2. A bounded **log ring buffer**. A logging.Handler appended in
   ``log_config.setup_logging`` mirrors every formatted log record into
   this buffer; the web UI polls it via ``/logs.json?since=<seq>`` for the
   live tail. The stdout handler stays in place — Docker still gets the
   stream.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional


_lock = threading.Lock()
_activity: dict = {
    "phase": "starting",
    "detail": None,
    "playlist_id": None,
    "playlist_name": None,
    "table_name": None,
    "track_id": None,
    "track_name": None,
    "track_artist": None,
    "since": time.time(),
}


def set_activity(
    phase: str,
    *,
    detail: Optional[str] = None,
    playlist_id: Optional[str] = None,
    playlist_name: Optional[str] = None,
    table_name: Optional[str] = None,
    track_id: Optional[str] = None,
    track_name: Optional[str] = None,
    track_artist: Optional[str] = None,
) -> None:
    """Replace the current activity record. ``since`` resets to now."""
    with _lock:
        _activity.update(
            phase=phase,
            detail=detail,
            playlist_id=playlist_id,
            playlist_name=playlist_name,
            table_name=table_name,
            track_id=track_id,
            track_name=track_name,
            track_artist=track_artist,
            since=time.time(),
        )


def get_activity() -> dict:
    """Return a snapshot of the current activity record."""
    with _lock:
        snap = dict(_activity)
    snap["age_seconds"] = max(0.0, time.time() - snap["since"])
    return snap


# --- log ring buffer -------------------------------------------------------

LOG_BUFFER_MAX = 2000

_log_lock = threading.Lock()
_log_seq = 0
_log_buffer: deque = deque(maxlen=LOG_BUFFER_MAX)


class RingBufferHandler(logging.Handler):
    """logging.Handler that appends formatted records to the in-memory buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        global _log_seq
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        entry = {
            "time": record.created,
            "level": record.levelname,
            "name": record.name,
            "message": message,
        }
        with _log_lock:
            _log_seq += 1
            entry["seq"] = _log_seq
            _log_buffer.append(entry)


def get_logs(since: int = 0, limit: int = 500) -> dict:
    """Return up to ``limit`` log records with seq > ``since``.

    Response shape:
        {"last_seq": int, "entries": [...]}

    The caller is expected to pass ``last_seq`` back as ``since`` next time.
    """
    with _log_lock:
        if since <= 0:
            entries = list(_log_buffer)[-limit:]
        else:
            entries = [e for e in _log_buffer if e["seq"] > since][-limit:]
        last = _log_seq
    return {"last_seq": last, "entries": entries}
