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

from . import config

# Process start, for the uptime on /system/status.
STARTED_AT = time.time()


def touch_heartbeat() -> None:
    """Update config.HEARTBEAT_FILE so the Docker HEALTHCHECK can tell a
    busy daemon from a wedged one. Never raises."""
    try:
        with open(config.HEARTBEAT_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        logging.debug("Could not write heartbeat file", exc_info=True)


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
    detail: str | None = None,
    playlist_id: str | None = None,
    playlist_name: str | None = None,
    table_name: str | None = None,
    track_id: str | None = None,
    track_name: str | None = None,
    track_artist: str | None = None,
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
_search_history: dict[str, deque] = {}


def record_search(
    track_id: str | None,
    query: str,
    *,
    result_count: int = 0,
    candidate_count: int | None = None,
    rejected_count: int | None = None,
    rejections: list | None = None,
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


def start_interactive_search(key: tuple[str, str]) -> object | None:
    """Claim the slot for ``key``. Returns an ownership token to pass to
    finish_interactive_search, or None if a search is already running.

    A search that went stale (INTERACTIVE_STALE_SECONDS) is replaced; its
    thread may still report back later, and the token is what makes that
    late finish a no-op instead of overwriting the newer search."""
    now = time.time()
    with _interactive_lock:
        for k in [k for k, e in _interactive.items() if _expired(e, now)]:
            del _interactive[k]
        current = _interactive.get(key)
        if current is not None and current["status"] == "running":
            return None
        token = object()
        _interactive[key] = {
            "token": token,
            "status": "running",
            "started": now,
            "finished": None,
            "results": [],
            "error": None,
        }
        return token


def finish_interactive_search(
    key: tuple[str, str],
    token: object,
    results: list[dict] | None = None,
    error: str | None = None,
) -> None:
    """Record the outcome — only if ``token`` still owns the slot."""
    with _interactive_lock:
        entry = _interactive.get(key)
        if entry is None or entry["token"] is not token:
            return
        entry.update(
            status="error" if error else "done",
            finished=time.time(),
            results=list(results or []),
            error=error,
        )


def get_interactive_search(key: tuple[str, str]) -> dict | None:
    """Snapshot of the search for ``key``, or None if absent/expired."""
    with _interactive_lock:
        entry = _interactive.get(key)
        if entry is None:
            return None
        if _expired(entry, time.time()):
            del _interactive[key]
            return None
        snap = dict(entry)
        snap.pop("token")
        return snap


# --- manual grabs: web UI -> daemon thread ---------------------------------
#
# The UI never downloads or writes the DB for a grab itself; it queues a
# request and wakes the daemon, which owns files and the DB writer.

_grab_lock = threading.Lock()
_grab_queue: deque = deque(maxlen=100)


def enqueue_manual_grab(request: dict) -> None:
    with _grab_lock:
        _grab_queue.append(dict(request))


def peek_manual_grabs() -> list:
    with _grab_lock:
        return [dict(r) for r in _grab_queue]


def pop_manual_grabs() -> list:
    with _grab_lock:
        items = list(_grab_queue)
        _grab_queue.clear()
    return items


# --- quarantine requests: web UI -> daemon thread --------------------------
#
# Like grabs: the UI only queues (table_name, track_id, flagged_path); the
# daemon moves the file and resets the row (library.health.quarantine_track),
# and skips the request if the row no longer points at flagged_path.

_quarantine_lock = threading.Lock()
_quarantine_queue: deque = deque(maxlen=5000)


def enqueue_quarantine(table_name: str, track_id: str, path: str | None) -> None:
    with _quarantine_lock:
        if not any(q[:2] == (table_name, track_id) for q in _quarantine_queue):
            _quarantine_queue.append((table_name, track_id, path))


def peek_quarantines() -> list:
    with _quarantine_lock:
        return list(_quarantine_queue)


def pop_quarantines() -> list:
    with _quarantine_lock:
        items = list(_quarantine_queue)
        _quarantine_queue.clear()
    return items


# --- rename requests: web UI -> daemon thread -------------------------------
#
# Library -> Rename: the UI queues (table_name, track_id, old_path, new_name)
# computed server-side from the current template; the daemon renames
# (library.rename.apply_rename) and skips a row that moved since.

_rename_lock = threading.Lock()
_rename_queue: deque = deque(maxlen=20000)


def enqueue_rename(table_name: str, track_id: str, old_path: str, new_name: str) -> None:
    with _rename_lock:
        if not any(q[:2] == (table_name, track_id) for q in _rename_queue):
            _rename_queue.append((table_name, track_id, old_path, new_name))


def peek_renames() -> list:
    with _rename_lock:
        return list(_rename_queue)


def pop_renames() -> list:
    with _rename_lock:
        items = list(_rename_queue)
        _rename_queue.clear()
    return items


def pending_renames() -> int:
    with _rename_lock:
        return len(_rename_queue)


# --- upgrade-check requests: web UI -> daemon thread ------------------------
#
# "Cutoff unmet" -> Search: the UI queues (table_name, track_id); the daemon
# runs library.acquire.try_upgrade_track for each at the top of the next
# cycle, whatever UPGRADE_CHECK_INTERVAL_HOURS says.

_upgrade_lock = threading.Lock()
_upgrade_queue: deque = deque(maxlen=5000)


def enqueue_upgrade_check(table_name: str, track_id: str) -> None:
    with _upgrade_lock:
        if (table_name, track_id) not in _upgrade_queue:
            _upgrade_queue.append((table_name, track_id))


def peek_upgrade_checks() -> list:
    with _upgrade_lock:
        return list(_upgrade_queue)


def pop_upgrade_checks() -> list:
    with _upgrade_lock:
        items = list(_upgrade_queue)
        _upgrade_queue.clear()
    return items


# --- the daemon's current transfer (queue page) -----------------------------
#
# Set by slskd.transfers.grab_candidate while it waits on a transfer, so the
# queue page can name the track a transfer is for before history has a row.

_transfer_lock = threading.Lock()
_current_transfer: dict | None = None


# Every enqueue the daemon made recently (newest last), so a transfer that
# finished or was left queued in slskd can still be named after the daemon
# moved on. Bounded; per-process like the rest of this module.
_recent_transfers: deque = deque(maxlen=500)


def set_current_transfer(info: dict | None) -> None:
    global _current_transfer
    with _transfer_lock:
        _current_transfer = dict(info) if info else None
        if info:
            _recent_transfers.append(dict(info))


def recent_transfers() -> list[dict]:
    with _transfer_lock:
        return [dict(t) for t in _recent_transfers]


def get_current_transfer() -> dict | None:
    with _transfer_lock:
        return dict(_current_transfer) if _current_transfer else None
