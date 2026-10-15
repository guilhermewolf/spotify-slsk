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


# --- per-track search history (debug aid for the track-detail page) -------
#
# Each entry is one slskd search the daemon performed for a given Spotify
# track id. Ephemeral — never written to the DB. Trades memory for the
# ability to ask "what queries did the daemon try for this track and how
# many results came back?" without diving into stdout logs.

SEARCH_HISTORY_PER_TRACK = 20
SEARCH_HISTORY_MAX_TRACKS = 500

_search_lock = threading.Lock()
_search_history: "dict[str, deque]" = {}


def record_search(
    track_id: Optional[str],
    query: str,
    *,
    result_count: int = 0,
    candidate_count: Optional[int] = None,
    rejected_count: Optional[int] = None,
    rejections: Optional[list] = None,
) -> None:
    """Record one query attempt for a track. No-op if ``track_id`` is falsy.

    ``candidate_count``/``rejected_count`` are the accepted and rejected
    files; ``rejections`` is a capped list of {user, filename, reason}.
    """
    if not track_id:
        return
    entry = {
        "time": time.time(),
        "query": query,
        "result_count": int(result_count),
        "candidate_count": (
            None if candidate_count is None else int(candidate_count)
        ),
        "rejected_count": None if rejected_count is None else int(rejected_count),
        "rejections": list(rejections or []),
    }
    with _search_lock:
        bucket = _search_history.get(track_id)
        if bucket is None:
            # When we'd exceed the global cap, drop the oldest tracked id
            # before allocating a new bucket. Insertion order is reliable
            # in Python 3.7+ dicts.
            if len(_search_history) >= SEARCH_HISTORY_MAX_TRACKS:
                oldest = next(iter(_search_history))
                _search_history.pop(oldest, None)
            bucket = deque(maxlen=SEARCH_HISTORY_PER_TRACK)
            _search_history[track_id] = bucket
        bucket.append(entry)


def get_search_history(track_id: str) -> list:
    """Return the recorded query attempts for one track (oldest first)."""
    if not track_id:
        return []
    with _search_lock:
        bucket = _search_history.get(track_id)
        return list(bucket) if bucket else []


# --- interactive search (track page) ---------------------------------------
#
# One slskd search per (playlist_id, track_id) at a time, run on a web-UI
# background thread. Results live here only — ephemeral, like the search
# history — and expire after INTERACTIVE_TTL_SECONDS.

INTERACTIVE_TTL_SECONDS = 600
# A search thread that never reported back (crash, hung slskd) stops
# blocking a new one after this long.
INTERACTIVE_STALE_SECONDS = 900

_interactive_lock = threading.Lock()
_interactive: dict = {}


def _expired(entry: dict, now: float) -> bool:
    if entry["status"] == "running":
        return now - entry["started"] > INTERACTIVE_STALE_SECONDS
    return now - entry["finished"] > INTERACTIVE_TTL_SECONDS


def start_interactive_search(key) -> bool:
    """Claim the slot for ``key``. False if a search is already running."""
    now = time.time()
    with _interactive_lock:
        for k in [k for k, e in _interactive.items() if _expired(e, now)]:
            del _interactive[k]
        current = _interactive.get(key)
        if current is not None and current["status"] == "running":
            return False
        _interactive[key] = {
            "status": "running",
            "started": now,
            "finished": None,
            "results": [],
            "error": None,
        }
        return True


def finish_interactive_search(key, results=None, error=None) -> None:
    with _interactive_lock:
        entry = _interactive.get(key)
        if entry is None:
            return
        entry.update(
            status="error" if error else "done",
            finished=time.time(),
            results=list(results or []),
            error=error,
        )


def get_interactive_search(key):
    """Snapshot of the search for ``key``, or None if absent/expired."""
    with _interactive_lock:
        entry = _interactive.get(key)
        if entry is None:
            return None
        if _expired(entry, time.time()):
            del _interactive[key]
            return None
        return dict(entry)


# --- manual grabs: web UI -> daemon thread ---------------------------------
#
# The UI never downloads or writes the DB for a grab itself; it queues a
# request and wakes the daemon, which owns files and the DB writer.

_grab_lock = threading.Lock()
_grab_queue: deque = deque(maxlen=100)


def enqueue_manual_grab(request: dict) -> None:
    with _grab_lock:
        _grab_queue.append(dict(request))


def pop_manual_grabs() -> list:
    with _grab_lock:
        items = list(_grab_queue)
        _grab_queue.clear()
    return items
