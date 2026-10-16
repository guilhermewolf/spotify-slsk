"""Notifications (Lidarr's Notifications): ntfy, a generic webhook, and a
Subsonic/Navidrome library rescan.

Events (config.NOTIFICATION_EVENTS) come from two places:

* history events (grabbed, imported, upgraded, download_failed,
  import_rejected, quarantined) via a db.history listener - so anything
  recorded in History can be notified without touching its call site;
* ``emit()`` from the daemon (playlist_synced, cycle_summary, health_issue,
  health_restored).

Nothing is sent where an event happens. Events queue in memory and the
daemon ``flush()``es them after each playlist and at the end of a cycle;
per-track events are batched into one message per (event, playlist), so a
playlist importing 40 tracks sends one "Imported 40 tracks" message, not 40.
Senders run on the daemon thread only (the settings page's Test button is
the one exception and touches no shared state).

Secrets (WEBHOOK_HEADER_VALUE, SUBSONIC_PASSWORD) and URLs - which can
carry tokens themselves (Discord/Slack webhooks, Subsonic's t/s query) - are
never logged: a failure logs the provider, the HTTP status or the exception
*type*, nothing else. /logs is unauthenticated.

A provider whose delivery fails is paused with a doubling backoff (1 min
up to 1 h); events for it during the pause are dropped, not queued.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import requests

from . import config
from .db import add_history_listener, get_playlist_meta, get_setting, get_track

TIMEOUT_SECONDS = 10
BACKOFF_MIN_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600
# Per-track events: batched per playlist, at most this many tracks listed.
HISTORY_EVENTS = frozenset(
    {"grabbed", "imported", "upgraded", "download_failed", "import_rejected", "quarantined"}
)
MAX_LISTED = 25
# Events after which a Subsonic server should rescan its library.
RESCAN_EVENTS = frozenset({"imported", "upgraded"})

DEFAULT_NTFY_EVENTS = "playlist_synced,cycle_summary,health_issue,health_restored"
DEFAULT_WEBHOOK_EVENTS = ",".join(config.NOTIFICATION_EVENTS)

LABELS = {
    "grabbed": "Grabbed",
    "imported": "Imported",
    "upgraded": "Upgraded",
    "download_failed": "Download failed",
    "import_rejected": "Import rejected",
    "quarantined": "Quarantined",
    "health_issue": "Health issue",
    "health_restored": "Health restored",
    "playlist_synced": "Playlist synced",
    "cycle_summary": "Cycle summary",
}

_lock = threading.Lock()
_queue: deque[dict[str, Any]] = deque(maxlen=2000)
_backoff: dict[str, tuple[float, float]] = {}  # provider -> (paused until, last delay)
_rescan_pending = False


# ---- producing events -------------------------------------------------------

def emit(event: str, **data: Any) -> None:
    """Queue an event for the next flush(). Thread-safe, never raises."""
    with _lock:
        _queue.append({"event": event, "ts": time.time(), "data": data})


def _on_history(event: str, playlist_id: str | None, track_id: str | None, data: dict[str, Any]) -> None:
    if event in HISTORY_EVENTS:
        emit(event, playlist_id=playlist_id, track_id=track_id, **data)


def install() -> None:
    """Start listening to history events (daemon startup)."""
    add_history_listener(_on_history)


def pending() -> int:
    with _lock:
        return len(_queue)


# ---- batching ---------------------------------------------------------------

def _describe_track(conn: sqlite3.Connection, table: str | None, track_id: str | None) -> str:
    row = get_track(conn, table, track_id) if table and track_id else None
    if row:
        return f"{row['name']} — {row['artists']}"
    return track_id or "?"


def batch(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn queued items into messages: [{event, ts, data, title, text}].
    History events are grouped per (event, playlist); the rest pass through
    in order."""
    messages: list[dict[str, Any]] = []
    groups: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for item in items:
        if item["event"] in HISTORY_EVENTS:
            key = (item["event"], item["data"].get("playlist_id"))
            if key not in groups:
                groups[key] = []
                messages.append({"group": key})
            groups[key].append(item)
        else:
            data = item["data"]
            messages.append({
                "event": item["event"], "ts": item["ts"], "data": data,
                "title": LABELS.get(item["event"], item["event"]),
                "text": data.get("message") or str(data),
            })
    out = []
    for msg in messages:
        if "group" not in msg:
            out.append(msg)
            continue
        event, playlist_id = msg["group"]
        group = groups[(event, playlist_id)]
        _, table, name, _ = get_playlist_meta(conn, playlist_id) if playlist_id else (None,) * 4
        tracks = []
        for item in group[:MAX_LISTED]:
            d = item["data"]
            entry = {"track_id": d.get("track_id"), "track": _describe_track(conn, table, d.get("track_id"))}
            for field in ("reason", "quality", "old_quality", "username", "filename", "problems"):
                if d.get(field):
                    entry[field] = d[field]
            tracks.append(entry)
        lines = [
            f"• {t['track']}" + (f" ({t['reason']})" if t.get("reason") else "")
            for t in tracks
        ]
        if len(group) > MAX_LISTED:
            lines.append(f"… and {len(group) - MAX_LISTED} more")
        where = f" in {name}" if name else ""
        out.append({
            "event": event,
            "ts": group[-1]["ts"],
            "data": {"playlist_id": playlist_id, "playlist": name, "count": len(group), "tracks": tracks},
            "title": f"{LABELS[event]}: {len(group)} track(s){where}",
            "text": "\n".join(lines),
        })
    return out


# ---- providers --------------------------------------------------------------

def _events(conn: sqlite3.Connection, key: str, default: str) -> set[str]:
    raw = get_setting(conn, key, default) or default
    return {e.strip() for e in raw.split(",") if e.strip()}


def _send_ntfy(conn: sqlite3.Connection, msg: dict[str, Any]) -> None:
    url = (get_setting(conn, "NTFY_URL", "") or "").rstrip("/")
    topic = get_setting(conn, "NTFY_TOPIC", "") or ""
    resp = requests.post(
        f"{url}/{topic}",
        data=msg["text"].encode("utf-8"),
        # Header values must be latin-1; the title is ours, so ASCII-fold it.
        headers={"Title": msg["title"].encode("ascii", "replace").decode()},
        timeout=TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if not resp.ok:
        raise DeliveryError(f"HTTP {resp.status_code}")


def _send_webhook(conn: sqlite3.Connection, msg: dict[str, Any]) -> None:
    headers = {"Content-Type": "application/json"}
    name = get_setting(conn, "WEBHOOK_HEADER_NAME", "") or ""
    value = get_setting(conn, "WEBHOOK_HEADER_VALUE", "") or ""
    if name and value:
        headers[name] = value
    resp = requests.post(
        get_setting(conn, "WEBHOOK_URL", ""),
        json={
            "event": msg["event"],
            "ts": datetime.fromtimestamp(msg["ts"], tz=UTC).isoformat(),
            "data": msg["data"],
        },
        headers=headers,
        timeout=TIMEOUT_SECONDS,
        # A redirect would carry the auth header to wherever it points.
        allow_redirects=False,
    )
    if not resp.ok:
        raise DeliveryError(f"HTTP {resp.status_code}")


class DeliveryError(Exception):
    """A provider answered, but not with success. The message is safe to log."""


Sender = Callable[[sqlite3.Connection, dict[str, Any]], None]

# name -> (label, is it configured?, events setting key, default events, sender)
PROVIDERS: dict[str, tuple[str, Callable[[sqlite3.Connection], bool], str, str, Sender]] = {
    "ntfy": (
        "ntfy",
        lambda c: bool(get_setting(c, "NTFY_URL", "") and get_setting(c, "NTFY_TOPIC", "")),
        "NTFY_EVENTS", DEFAULT_NTFY_EVENTS, _send_ntfy,
    ),
    "webhook": (
        "Webhook",
        lambda c: bool(get_setting(c, "WEBHOOK_URL", "")),
        "WEBHOOK_EVENTS", DEFAULT_WEBHOOK_EVENTS, _send_webhook,
    ),
}


def safe_error(e: Exception) -> str:
    """What may be logged or shown about a failed request: never str(e),
    which for requests' errors includes the full URL (query included)."""
    return str(e) if isinstance(e, DeliveryError) else type(e).__name__


def _deliver(name: str, conn: sqlite3.Connection, msg: dict[str, Any], now: float) -> None:
    until, delay = _backoff.get(name, (0.0, 0.0))
    if now < until:
        return
    try:
        PROVIDERS[name][4](conn, msg)
    except Exception as e:
        delay = min(max(BACKOFF_MIN_SECONDS, delay * 2), BACKOFF_MAX_SECONDS)
        _backoff[name] = (now + delay, delay)
        logging.warning(
            f"[notify] {PROVIDERS[name][0]} delivery failed ({safe_error(e)}); "
            f"pausing it for {int(delay)}s"
        )
        return
    _backoff.pop(name, None)


def flush(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Send everything queued. Daemon thread only. Returns messages built."""
    global _rescan_pending
    with _lock:
        items = list(_queue)
        _queue.clear()
    if not items:
        return 0
    now = now if now is not None else time.time()
    if any(i["event"] in RESCAN_EVENTS for i in items):
        _rescan_pending = True
    try:
        messages = batch(conn, items)
    except Exception:
        logging.exception("[notify] Could not build notifications; dropping them")
        return 0
    for name, (_label, configured, events_key, default, _send) in PROVIDERS.items():
        if not configured(conn):
            continue
        wanted = _events(conn, events_key, default)
        for msg in messages:
            if msg["event"] in wanted:
                _deliver(name, conn, msg, now)
    return len(messages)


# ---- Subsonic / Navidrome ----------------------------------------------------

def subsonic_configured(conn: sqlite3.Connection) -> bool:
    return all(get_setting(conn, k, "") for k in ("SUBSONIC_URL", "SUBSONIC_USERNAME", "SUBSONIC_PASSWORD"))


def subsonic_start_scan(conn: sqlite3.Connection) -> None:
    """GET /rest/startScan with token auth (t = md5(password + salt)), so
    the password itself never travels. Raises DeliveryError on a refusal."""
    salt = secrets.token_hex(8)
    password = get_setting(conn, "SUBSONIC_PASSWORD", "") or ""
    resp = requests.get(
        (get_setting(conn, "SUBSONIC_URL", "") or "").rstrip("/") + "/rest/startScan",
        params={
            "u": get_setting(conn, "SUBSONIC_USERNAME", ""),
            "t": hashlib.md5((password + salt).encode(), usedforsecurity=False).hexdigest(),
            "s": salt,
            "v": "1.16.1",
            "c": "spotify-slsk",
            "f": "json",
        },
        timeout=TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if not resp.ok:
        raise DeliveryError(f"HTTP {resp.status_code}")
    try:
        body = resp.json().get("subsonic-response") or {}
    except ValueError:
        raise DeliveryError("not a Subsonic response") from None
    if body.get("status") != "ok":
        error = body.get("error") or {}
        raise DeliveryError(f"Subsonic error {error.get('code')}: {error.get('message')}")


def rescan_library(conn: sqlite3.Connection) -> bool:
    """Once per cycle, after imports: ask the Subsonic server to rescan.
    Debounced: only when something was imported or upgraded since the last
    call. Returns True when a scan was requested."""
    global _rescan_pending
    if not _rescan_pending or not subsonic_configured(conn):
        return False
    _rescan_pending = False
    now = time.time()
    until, delay = _backoff.get("subsonic", (0.0, 0.0))
    if now < until:
        return False
    try:
        subsonic_start_scan(conn)
    except Exception as e:
        delay = min(max(BACKOFF_MIN_SECONDS, delay * 2), BACKOFF_MAX_SECONDS)
        _backoff["subsonic"] = (now + delay, delay)
        logging.warning(f"[notify] Subsonic rescan failed ({safe_error(e)}); pausing it for {int(delay)}s")
        return False
    _backoff.pop("subsonic", None)
    logging.info("[notify] Asked the Subsonic server to rescan its library")
    return True


def send_test(conn: sqlite3.Connection, provider: str) -> str:
    """The settings page's Test button: one message, now, bypassing the
    queue and the backoff. Returns a safe description; raises on failure."""
    if provider == "subsonic":
        if not subsonic_configured(conn):
            raise DeliveryError("SUBSONIC_URL, SUBSONIC_USERNAME and SUBSONIC_PASSWORD must all be set")
        subsonic_start_scan(conn)
        return "Subsonic accepted a library scan"
    label, configured, _key, _default, send = PROVIDERS[provider]
    if not configured(conn):
        raise DeliveryError(f"{label} is not configured")
    send(conn, {
        "event": "test", "ts": time.time(), "title": "spotify-slsk test",
        "text": "Test notification from spotify-slsk", "data": {"message": "test"},
    })
    return f"{label} accepted the test notification"


# ---- health transitions ------------------------------------------------------

HEALTH_STATE_KEY = "_health_state"


def health_transitions(previous: dict[str, str], checks: list[dict[str, str]]) -> None:
    """Emit health_issue / health_restored for checks whose status changed.
    A check seen for the first time only notifies when it is not ok."""
    for check in checks:
        before = previous.get(check["name"], "ok")
        now = check["status"]
        if now == before:
            continue
        if now == "ok":
            emit("health_restored", check=check["name"], status=now,
                 message=f"{check['name']}: {check['message']}")
        else:
            emit("health_issue", check=check["name"], status=now,
                 message=f"{check['name']} ({now}): {check['message']}")
