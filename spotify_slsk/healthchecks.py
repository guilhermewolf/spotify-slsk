"""Structured health checks (Lidarr's HealthCheck), for /healthz,
/system/status and the health_issue / health_restored notifications.

Two kinds:

* **Local** checks are a stat(), an access() or a table read - cheap enough
  to run on every /healthz request.
* **Remote** checks (slskd, Spotify) make a network call, so only the
  daemon runs them, as the ``health_checks`` task, and the result is kept in
  memory (``_remote``) for the UI to read. /healthz never waits on a remote
  service: it must answer when that service is slow.

The database integrity check is its own daily task (``db_integrity``); its
verdict is that task's last result.

None of this changes /healthz's ``ok`` (schema version + heartbeat), which
is what Docker and any external monitor already key on.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from typing import Any

from . import config
from .db import list_task_rows

OK, WARN, ERROR = "ok", "warn", "error"

# A completed cycle older than this is reported. Cycles with a download
# backlog legitimately run for many hours, hence the generous bound.
CYCLE_STALE_SECONDS = 48 * 3600

Check = dict[str, str]


def _check(name: str, status: str, message: str) -> Check:
    return {"name": name, "status": status, "message": message}


def _dir_check(name: str, path: str) -> Check:
    if not os.path.isdir(path):
        return _check(name, ERROR, f"{path} does not exist")
    if not os.access(path, os.W_OK | os.X_OK):
        return _check(name, ERROR, f"{path} is not writable by uid {os.getuid()}")
    return _check(name, OK, f"{path} is writable")


def disk_usage(path: str) -> tuple[int, int] | None:
    """(free, total) bytes of the volume holding `path`, or None."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.free, usage.total


def _disk_check(path: str) -> Check:
    usage = disk_usage(path)
    if usage is None:
        return _check("disk_space", ERROR, f"cannot read free space of {path}")
    free_gb = usage[0] / 1024**3
    minimum = config.MIN_FREE_SPACE_GB
    if minimum > 0 and free_gb < minimum:
        return _check("disk_space", WARN, f"{free_gb:.1f} GB free on {path} (below {minimum} GB)")
    return _check("disk_space", OK, f"{free_gb:.1f} GB free on {path}")


def _heartbeat_check() -> Check:
    path = os.getenv("HEARTBEAT_FILE", "/tmp/heartbeat")
    stale = int(os.getenv("HEARTBEAT_STALE_SECONDS", "900"))
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return _check("heartbeat", WARN, "waiting for the daemon's first heartbeat")
    if age >= stale:
        return _check("heartbeat", ERROR, f"no daemon heartbeat for {int(age)}s (limit {stale}s)")
    return _check("heartbeat", OK, f"daemon active {int(age)}s ago")


def _cycle_check(conn: sqlite3.Connection, now: float) -> Check:
    row = list_task_rows(conn).get("spotify_sync") or {}
    if not row.get("last_run"):
        return _check("last_cycle", OK, "no cycle completed yet")
    ended = row["last_run"] + (row.get("last_duration") or 0)
    age = now - ended
    hours = age / 3600
    if age > CYCLE_STALE_SECONDS:
        return _check("last_cycle", WARN, f"last cycle completed {hours:.0f} h ago")
    return _check("last_cycle", OK, f"last cycle completed {hours:.1f} h ago")


def _flac_check() -> Check | None:
    if not config.LOSSLESS_CHECK_ENABLED:
        return None
    if shutil.which("flac") is None:
        return _check(
            "flac_binary", WARN,
            "flac is not installed: the fake-lossless check judges every FLAC 'unknown'",
        )
    return _check("flac_binary", OK, "flac is installed")


def _integrity_check(conn: sqlite3.Connection) -> Check | None:
    row = list_task_rows(conn).get("db_integrity") or {}
    if not row.get("last_run"):
        return None
    if row.get("last_ok"):
        return _check("db_integrity", OK, row.get("last_result") or "ok")
    return _check("db_integrity", ERROR, row.get("last_result") or "failed")


LOOPBACK_ADDRS = ("127.0.0.1", "::1", "localhost")


def _ui_exposure_check(conn: sqlite3.Connection) -> Check | None:
    """The UI listening beyond loopback with no login. Inside a container
    0.0.0.0 is normal (the port mapping decides who can reach it), so this
    is a warning to check the mapping, not an error."""
    if os.getenv("UI_ENABLED", "1") != "1":
        return None
    bind = os.getenv("UI_BIND_ADDR", "0.0.0.0")
    if config.ui_auth_configured(conn):
        return _check("ui_auth", OK, "web UI login is enabled")
    if bind in LOOPBACK_ADDRS:
        return _check("ui_auth", OK, f"web UI bound to {bind} only")
    return _check(
        "ui_auth", WARN,
        f"web UI listens on {bind} without a login: anyone who can reach the port can "
        "change settings and delete playlists. Fine if the port is published on "
        "127.0.0.1 only; otherwise set UI_AUTH_USERNAME / UI_AUTH_PASSWORD",
    )


def local_checks(conn: sqlite3.Connection, now: float | None = None) -> list[Check]:
    now = now if now is not None else time.time()
    playlists = os.getenv("SLSKD_PLAYLISTS_DIR", "/playlists")
    checks = [
        _dir_check("downloads_dir", config.DOWNLOAD_DIR),
        _dir_check("playlists_dir", playlists),
        _disk_check(playlists),
        _heartbeat_check(),
        _cycle_check(conn, now),
        _flac_check(),
        _integrity_check(conn),
        _ui_exposure_check(conn),
    ]
    return [c for c in checks if c is not None]


# ---- remote checks (daemon thread only) -----------------------------------

_remote_lock = threading.Lock()
_remote: list[Check] = []


def _slskd_check() -> Check:
    from .slskd import client as slskd_client

    if not os.getenv("SLSKD_API_KEY"):
        return _check("slskd", ERROR, "SLSKD_API_KEY is not set")
    try:
        state = slskd_client.get_client().application.state()
        server = state.get("server") or {}
    except Exception as e:
        return _check("slskd", ERROR, f"slskd unreachable: {type(e).__name__}")
    if not server.get("isConnected"):
        return _check("slskd", ERROR, "slskd is not connected to the Soulseek server")
    if not server.get("isLoggedIn"):
        return _check("slskd", ERROR, "slskd is connected but not logged in")
    return _check("slskd", OK, "connected and logged in")


def _spotify_check(sp: Any) -> Check:
    if sp is None:
        return _check(
            "spotify", ERROR, "no Spotify client: set SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET"
        )
    try:
        sp.auth_manager.get_access_token(as_dict=False)
    except Exception as e:
        # The exception names the OAuth error, never the secret.
        return _check("spotify", ERROR, f"Spotify credentials rejected: {type(e).__name__}")
    return _check("spotify", OK, "credentials valid")


def refresh_remote(sp: Any) -> list[Check]:
    """Run the remote checks and keep the result for the UI. Daemon only:
    it uses the daemon's shared slskd client."""
    checks = [_slskd_check(), _spotify_check(sp)]
    with _remote_lock:
        _remote[:] = checks
    return checks


def remote_checks() -> list[Check]:
    with _remote_lock:
        return [dict(c) for c in _remote]


def collect(conn: sqlite3.Connection) -> list[Check]:
    """Every check, remote ones as of their last run."""
    return remote_checks() + local_checks(conn)


def summary(checks: list[Check]) -> str:
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in (OK, WARN, ERROR)}
    return ", ".join(f"{n} {s}" for s, n in counts.items() if n) or "no checks"


def integrity_check(conn: sqlite3.Connection) -> tuple[bool, str]:
    """PRAGMA quick_check: (ok, message)."""
    try:
        rows = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.Error as e:
        logging.error(f"[health] Integrity check failed to run: {e}")
        return False, f"quick_check failed: {e}"
    if rows == ["ok"]:
        return True, "quick_check ok"
    return False, "quick_check: " + "; ".join(rows[:5])
