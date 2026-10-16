"""The daemon: startup order, the cycle loop, heartbeat, shutdown and wake.

``main()`` runs on the main thread and is the only writer of library files
and of the DB's track rows (the web UI only queues requests - see
``runtime_state.enqueue_manual_grab`` and ``wake_now``). The startup order
in ``main()`` is load-bearing; CLAUDE.md explains each step.
"""
from __future__ import annotations

import logging
import os
import signal
import sqlite3
import threading
import time
from collections.abc import Callable

import spotipy

from . import config, healthchecks, runtime_state, tasks
from .db import (
    clear_tried_entries,
    create_connection,
    delete_setting,
    get_pending_tracks,
    get_playlist_meta,
    get_setting,
    get_track,
    get_upgrade_candidates,
    list_playlists,
    mark_upgrade_checked,
    pop_queued_tasks,
    prune_history,
    queue_task,
    record_cycle,
    record_task_run,
    set_setting,
)
from .importer.process import process_downloaded_file
from .library import health, playlist_files, rename
from .library.acquire import handle_track_download, try_upgrade_track
from .library.reconcile import startup_check
from .log_config import setup_logging
from .models import Track
from .notify import send_ntfy_notification
from .slskd import client as slskd_client
from .slskd import transfers
from .slskd.client import wait_for_slskd_healthy
from .spotify.client import setup_spotify_client
from .spotify.sync import fetch_and_compare_tracks, migrate_env_playlists

_shutdown = threading.Event()
# Set by wake_now() to break the inter-cycle sleep early. Cleared at the
# top of each cycle loop iteration after the wait returns.
_wake_event = threading.Event()


def _touch_heartbeat():
    """Update the heartbeat file so Docker HEALTHCHECK can detect a wedged loop."""
    runtime_state.touch_heartbeat()


def _install_signal_handlers():
    def _handle(signum, _frame):
        logging.info(f"Received signal {signum}, initiating graceful shutdown")
        _shutdown.set()
        # Also break the cycle waiter so shutdown is prompt.
        _wake_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle)


def wake_now() -> None:
    """Break the cycle sleep early so the next cycle starts immediately.

    Called by the webui after state-changing actions (retry, refresh,
    enable/add) so the user doesn't wait up to config.CYCLE_INTERVAL_SECONDS for
    the change to be observable. Idempotent — repeated calls within the
    same sleep window collapse to one wake-up.
    """
    _wake_event.set()


def block_until_slskd_healthy(host: str, api_key: str | None, retry_seconds: float = 60) -> bool:
    """Keep waiting for slskd, staying alive and heartbeating while we do.

    Cycles must not start before slskd is reachable: every download would
    fail, and two failures suspend a track for two days (see
    db.MAX_ATTEMPTS_BEFORE_SUSPEND). So a dependency outage would otherwise
    convert into a self-inflicted two-day backlog.

    Returns True when healthy, False if shutdown was requested first.
    """
    while not _shutdown.is_set():
        # Keep the heartbeat fresh: the process is alive and doing its job,
        # it is a dependency that is down. Letting the file go stale would
        # have Docker restart us repeatedly for someone else's outage.
        _touch_heartbeat()
        if wait_for_slskd_healthy(host, api_key):
            return True
        logging.warning(
            f"slskd still unreachable; retrying in {retry_seconds}s. "
            f"The dashboard stays available at /healthz."
        )
        if _shutdown.wait(retry_seconds):
            break
    return False


def process_playlist(
    sp: spotipy.Spotify,
    conn: sqlite3.Connection,
    playlist_id: str,
    ntfy_url: str | None,
    ntfy_topic: str | None,
) -> dict[str, int]:
    """Sync + download for one playlist. Returns per-playlist counters
    (tracks_added/downloaded/failed) so the caller can aggregate them
    into a cycle_history row.
    """
    logging.info(f"Processing playlist ID: {playlist_id}")
    _touch_heartbeat()

    # Best-effort name lookup so the activity badge has something readable
    # before the Spotify fetch returns.
    _, _stored_table, stored_name, _ = get_playlist_meta(conn, playlist_id)
    runtime_state.set_activity(
        "syncing",
        detail="Fetching playlist from Spotify",
        playlist_id=playlist_id,
        playlist_name=stored_name,
        table_name=_stored_table,
    )

    new_tracks, playlist_name = fetch_and_compare_tracks(conn, playlist_id, sp)
    counters = {
        "tracks_added": len(new_tracks),
        "tracks_downloaded": 0,
        "tracks_failed": 0,
    }

    if new_tracks:
        msg = f"Playlist updated: {len(new_tracks)} new track(s) added to {playlist_name}"
        send_ntfy_notification(ntfy_url, ntfy_topic, msg)

    tracks = get_pending_tracks(conn, playlist_name)
    if tracks:
        for track in tracks:
            if _shutdown.is_set():
                logging.info("Shutdown requested, stopping track processing")
                return counters
            # One track can occupy several minutes (60s search + up to 300s
            # transfer + 60s post-processing, per candidate). Touching the
            # heartbeat per track is what lets the Docker HEALTHCHECK tell a
            # slow-but-working daemon from a wedged one.
            _touch_heartbeat()
            runtime_state.set_activity(
                "downloading",
                detail=f"{track.name} — {track.artist}",
                playlist_id=playlist_id,
                playlist_name=playlist_name,
                table_name=playlist_name,
                track_id=track.id,
                track_name=track.name,
                track_artist=track.artist,
            )
            logging.info(f"Downloading: {track.name} by {track.artist}")
            success = handle_track_download(track, playlist_name, conn)
            if success:
                counters["tracks_downloaded"] += 1
                logging.info(f"Downloaded: {track.name} by {track.artist}")
            else:
                counters["tracks_failed"] += 1
                logging.warning(f"Failed: {track.name} by {track.artist}")
    else:
        logging.info(f"No pending tracks for {playlist_name}")

    runtime_state.set_activity(
        "upgrading",
        detail="Checking for higher-tier versions",
        playlist_id=playlist_id,
        playlist_name=playlist_name,
        table_name=playlist_name,
    )
    _run_upgrade_pass(conn, playlist_name)
    playlist_files.write_playlist_files(conn, playlist_id)

    send_ntfy_notification(ntfy_url, ntfy_topic, f"Finished processing playlist: {playlist_name}")
    return counters


def _run_upgrade_pass(conn, playlist_name):
    """Per-playlist upgrade pass. Throttled by UPGRADE_CHECK_INTERVAL_HOURS."""
    try:
        interval = int(get_setting(conn, "UPGRADE_CHECK_INTERVAL_HOURS", "168"))
    except (TypeError, ValueError):
        interval = 168
    if interval <= 0:
        return

    candidates = get_upgrade_candidates(conn, playlist_name, interval)
    if not candidates:
        return

    logging.info(
        f"[upgrade] {playlist_name}: {len(candidates)} track(s) eligible for upgrade check "
        f"(interval={interval}h)"
    )
    for track, current_path in candidates:
        if _shutdown.is_set():
            logging.info("[upgrade] Shutdown requested, stopping upgrade pass")
            return
        _touch_heartbeat()
        try:
            try_upgrade_track(track, playlist_name, conn, current_path)
        except Exception:
            logging.exception(
                f"[upgrade] Failed to check track {track.id} in {playlist_name}"
            )
        finally:
            mark_upgrade_checked(conn, playlist_name, track.id)


def _process_manual_grabs(conn):
    """Download grabs queued from the track page's interactive search.

    Runs on the daemon thread so only it writes files and the DB. Reuses
    the automatic path: grab_candidate (tried/blocklist/history on
    failure) then process_downloaded_file (tag check, move, import). A
    manual failure does not count toward the track's attempts.
    """
    for req in runtime_state.pop_manual_grabs():
        if _shutdown.is_set():
            return
        table_name, candidate = req["table_name"], req["candidate"]
        row = get_track(conn, table_name, req["track_id"])
        if not row:
            continue
        track = Track(
            row["id"], row["name"], row["artists"], row["album"], table_name,
            duration_ms=row["duration_ms"],
        )
        _touch_heartbeat()
        runtime_state.set_activity(
            "downloading",
            detail=f"Manual grab: {track.name} — {track.artist}",
            playlist_id=req["playlist_id"],
            table_name=table_name,
            track_id=track.id,
            track_name=track.name,
            track_artist=track.artist,
        )
        try:
            file_path = transfers.grab_candidate(
                candidate, conn, table_name, track.id, source="manual"
            )
            if not file_path:
                continue
            verified, final_path = process_downloaded_file(
                file_path, table_name, conn, expected_track=track, source=candidate
            )
            if not verified or not final_path:  # verified implies a path
                continue
            clear_tried_entries(conn, table_name, track.id)
            # The import is verified against this track only, but re-read the
            # row anyway: replace the old file only if the import landed here.
            imported = get_track(conn, table_name, track.id) or {}
            old = row["path"]
            if (
                imported.get("path") == final_path
                and old
                and os.path.isfile(old)
                and os.path.abspath(old) != os.path.abspath(final_path)
            ):
                health.retire_replaced_file(
                    conn, table_name, track.id, old, "replaced by manual grab"
                )
        except Exception:
            logging.exception(f"Manual grab failed for {track.id} in {table_name}")


def _process_upgrade_requests(conn: sqlite3.Connection) -> None:
    """Upgrade checks queued from Wanted -> Cutoff unmet. They bypass the
    UPGRADE_CHECK_INTERVAL_HOURS throttle (and run even when it is 0) but
    stamp it like the regular pass."""
    for table_name, track_id in runtime_state.pop_upgrade_checks():
        if _shutdown.is_set():
            return
        row = get_track(conn, table_name, track_id)
        if not row or not row["downloaded"]:
            continue
        track = Track(
            row["id"], row["name"], row["artists"], row["album"], table_name,
            duration_ms=row["duration_ms"], isrc=row["isrc"],
        )
        _touch_heartbeat()
        runtime_state.set_activity(
            "upgrading",
            detail=f"Requested upgrade check: {track.name} — {track.artist}",
            table_name=table_name,
            track_id=track.id,
            track_name=track.name,
            track_artist=track.artist,
        )
        try:
            try_upgrade_track(track, table_name, conn, row["path"])
        except Exception:
            logging.exception(f"[upgrade] Requested check failed for {track_id} in {table_name}")
        finally:
            mark_upgrade_checked(conn, table_name, track_id)


def _process_quarantines(conn: sqlite3.Connection) -> None:
    """Quarantine library files the operator flagged on /library-health.
    Runs before the playlists so the reset rows re-download this cycle."""
    for table_name, track_id, path in runtime_state.pop_quarantines():
        if _shutdown.is_set():
            return
        _touch_heartbeat()
        try:
            health.quarantine_track(conn, table_name, track_id, path)
        except Exception:
            logging.exception(f"Quarantine failed for {track_id} in {table_name}")


def _process_renames(conn: sqlite3.Connection) -> None:
    """Renames applied from Library -> Rename (library.rename)."""
    for table_name, track_id, old_path, new_name in runtime_state.pop_renames():
        if _shutdown.is_set():
            return
        _touch_heartbeat()
        try:
            rename.apply_rename(conn, table_name, track_id, old_path, new_name)
        except Exception:
            logging.exception(f"Rename failed for {track_id} in {table_name}")


# Between playlists the scan and the quality backfill get a small budget, so
# a cycle that spends hours downloading still makes progress on them without
# delaying the next playlist by more than ~30 s.
BETWEEN_PLAYLISTS_SCAN_FILES = 50
BETWEEN_PLAYLISTS_SCAN_SECONDS = 30.0
BETWEEN_PLAYLISTS_BACKFILL_FILES = 50


def _run_library_scan(
    conn: sqlite3.Connection,
    max_files: int = health.MAX_FILES_PER_STEP,
    max_seconds: float = health.MAX_SECONDS_PER_STEP,
) -> None:
    """One bounded step of the library health scan, when one is requested
    (UI button) or due (LIBRARY_SCAN_INTERVAL_HOURS). Report-only."""
    pending = get_setting(conn, health.SCAN_PENDING_KEY, "0") == "1"
    try:
        last = float(get_setting(conn, health.SCAN_COMPLETED_KEY, "0") or 0)
    except ValueError:
        last = 0.0
    interval = config.LIBRARY_SCAN_INTERVAL_HOURS
    due = interval > 0 and time.time() - last >= interval * 3600
    if not (pending or due):
        return
    runtime_state.set_activity("scanning", detail="Library health scan")
    started = time.time()
    checked, finished = health.scan_step(
        conn, should_stop=_shutdown.is_set, heartbeat=_touch_heartbeat,
        max_files=max_files, max_seconds=max_seconds,
    )
    result = f"checked {checked} file(s); {'scan complete' if finished else 'continuing'}"
    logging.info(f"[health] {result}")
    record_task_run(
        conn, "library_scan", started=started, duration=time.time() - started, result=result
    )
    if finished:
        delete_setting(conn, health.SCAN_PENDING_KEY)
        set_setting(conn, health.SCAN_COMPLETED_KEY, str(time.time()))


def _drain_queues(conn: sqlite3.Connection) -> None:
    """Everything the web UI queued for the daemon: quarantines first (so
    the reset rows re-download this cycle), then renames, manual grabs and
    requested upgrade checks. Runs at the start of a cycle and between
    playlists, so nothing the operator asked for waits a whole cycle."""
    _process_quarantines(conn)
    _process_renames(conn)
    _process_manual_grabs(conn)
    _process_upgrade_requests(conn)


def _between_playlists(conn: sqlite3.Connection) -> None:
    """Queued requests plus a small step of the background library work.
    The library scan used to run only at the end of a cycle, so a cycle
    with a download backlog (hours) starved it."""
    _drain_queues(conn)
    _run_scheduled(conn)
    if _shutdown.is_set():
        return
    _run_library_scan(
        conn, max_files=BETWEEN_PLAYLISTS_SCAN_FILES,
        max_seconds=BETWEEN_PLAYLISTS_SCAN_SECONDS,
    )
    if _shutdown.is_set():
        return
    health.backfill_qualities(
        conn, max_files=BETWEEN_PLAYLISTS_BACKFILL_FILES, heartbeat=_touch_heartbeat
    )


def _run_cycle(sp, conn: sqlite3.Connection, playlists: list, ntfy_url, ntfy_topic) -> dict[str, int]:
    """One cycle: queued requests, each playlist (with the between-playlist
    step after it), then the full-budget library work and housekeeping.
    Returns the summed per-playlist counters."""
    cycle_totals = {"tracks_added": 0, "tracks_downloaded": 0, "tracks_failed": 0}
    # A "Run now" of a cycle-driven task is answered by this cycle.
    pop_queued_tasks(conn, ["spotify_sync", "quality_backfill"])
    _drain_queues(conn)
    _run_scheduled(conn)
    for pl in playlists:
        if _shutdown.is_set():
            break
        counters = process_playlist(sp, conn, pl["playlist_id"], ntfy_url, ntfy_topic)
        for k in cycle_totals:
            cycle_totals[k] += (counters or {}).get(k, 0)
        if not _shutdown.is_set():
            _between_playlists(conn)
    if not _shutdown.is_set():
        _run_library_scan(conn)
    if not _shutdown.is_set():
        started = time.time()
        done = health.backfill_qualities(conn, heartbeat=_touch_heartbeat)
        record_task_run(
            conn, "quality_backfill", started=started, duration=time.time() - started,
            result=f"read the quality of {done} file(s)",
        )
    if not _shutdown.is_set():
        _run_scheduled(conn)
    return cycle_totals


def _purge_recycle_bin(conn: sqlite3.Connection) -> str:
    """Daily when RECYCLE_BIN_RETENTION_DAYS > 0 (see tasks.TASKS)."""
    days = config.RECYCLE_BIN_RETENTION_DAYS
    purged = health.purge_recycle_bin(conn, days)
    logging.info(f"[recycle] Purged {purged} file(s) older than {days} day(s)")
    return f"purged {purged} file(s) older than {days} day(s)"


# The daemon's Spotify client, for the health checks (set by main()).
_spotify: spotipy.Spotify | None = None


def _db_integrity(conn: sqlite3.Connection) -> str:
    """Daily PRAGMA quick_check. Raises on a problem so the task records a
    failure, which the db_integrity health check reports."""
    ok, message = healthchecks.integrity_check(conn)
    if not ok:
        raise RuntimeError(message)
    return message


def _health_checks(conn: sqlite3.Connection) -> str:
    """Refresh the remote checks (slskd, Spotify) for /healthz and the
    status page."""
    healthchecks.refresh_remote(_spotify)
    return healthchecks.summary(healthchecks.collect(conn))


# What each scheduled task does. Tasks driven by the cycle itself
# (spotify_sync, library_scan, quality_backfill) are recorded where they run.
SCHEDULED: dict[str, Callable[[sqlite3.Connection], str]] = {
    "recycle_purge": _purge_recycle_bin,
    "db_integrity": _db_integrity,
    "health_checks": _health_checks,
}


def _run_task(conn: sqlite3.Connection, name: str, fn: Callable[[sqlite3.Connection], str]) -> None:
    """Run one task, timing it and recording the outcome. Never raises: a
    failing task must not fail the cycle."""
    started = time.time()
    runtime_state.set_activity("task", detail=tasks.BY_NAME[name].label)
    _touch_heartbeat()
    try:
        result, ok = fn(conn), True
    except Exception as e:
        logging.exception(f"[tasks] {name} failed")
        result, ok = f"failed: {e}", False
    record_task_run(
        conn, name, started=started, duration=time.time() - started, result=result, ok=ok
    )


def _run_scheduled(conn: sqlite3.Connection) -> None:
    """Run every scheduled task that is due or was requested (Run now)."""
    requested = pop_queued_tasks(conn, list(SCHEDULED))
    for name, fn in SCHEDULED.items():
        if _shutdown.is_set():
            return
        if name in requested or tasks.is_due(conn, name):
            _run_task(conn, name, fn)


def _run_startup_reconciliation(sp, conn):
    """Run the one-shot on-disk reconciliation once per daemon start."""
    for pl in list_playlists(conn, only_enabled=True):
        if _shutdown.is_set():
            return
        try:
            _, playlist_name = fetch_and_compare_tracks(conn, pl["playlist_id"], sp)
            startup_check(conn, playlist_name)
        except Exception:
            logging.exception(
                f"Startup reconciliation failed for {pl['playlist_id']} ({pl['name']})"
            )


def main() -> None:
    # Persist the Spotipy auth token across restarts on the data volume
    # (read by setup_spotify_client's cache handler).
    os.environ.setdefault("SPOTIPY_CACHE_PATH", "/app/data/.spotipy-cache")

    setup_logging()
    _install_signal_handlers()
    slskd_client.set_shutdown_event(_shutdown)
    logging.info("Starting main process")
    runtime_state.set_activity("starting", detail="Daemon booting")

    slskd_api_key = os.getenv("SLSKD_API_KEY")
    slskd_host_url = os.getenv("SLSKD_HOST_URL", "http://slskd:5030")
    ntfy_url = os.getenv("NTFY_URL")
    ntfy_topic = os.getenv("NTFY_TOPIC")


    conn = create_connection("./data/playlist_tracks.db")
    if not conn:
        logging.error("Failed to connect to the SQLite database.")
        return

    global _spotify
    try:
        sp = _spotify = setup_spotify_client()
        if sp is not None:
            migrate_env_playlists(conn, sp)
        config.ensure_quality_profile(conn)
        config.reload(conn)

        # Bring the dashboard up before waiting on slskd. Previously the
        # slskd wait ran first and raised on timeout, so an slskd outage took
        # the whole process down — and with it the UI and /healthz the
        # operator needed to diagnose the outage — leaving Docker to
        # crash-loop the container every ~90s.
        if os.getenv("UI_ENABLED", "1") == "1":
            from .webui import run_in_thread as _run_webui
            _run_webui(
                db_path="./data/playlist_tracks.db",
                spotify_client=sp,
                wake_callback=wake_now,
                host=os.getenv("UI_BIND_ADDR", "0.0.0.0"),
                port=int(os.getenv("UI_PORT", "8000")),
            )

        # A first verdict for /healthz and the status page before the
        # (possibly long) wait for slskd - that wait is when it is wanted.
        _run_task(conn, "health_checks", _health_checks)
        runtime_state.set_activity(
            "waiting_slskd", detail=f"Waiting for slskd at {slskd_host_url}"
        )
        if not block_until_slskd_healthy(slskd_host_url, slskd_api_key):
            return  # shutdown requested while waiting
        queue_task(conn, "health_checks")  # slskd is back: say so promptly

        send_ntfy_notification(
            ntfy_url, ntfy_topic, "Spotify Playlist Downloader starting"
        )
        # Both of these need Spotify. Their own try/except would swallow the
        # resulting AttributeError, but the operator would then get a
        # 'NoneType' has no attribute 'playlist' traceback per playlist per
        # boot instead of being told the actual problem.
        if sp is not None:
            runtime_state.set_activity(
                "reconciling", detail="Matching local files to DB"
            )
            _run_startup_reconciliation(sp, conn)
        else:
            logging.error(
                "Skipping startup reconciliation: no Spotify client. Set "
                "SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET and restart."
            )

        while not _shutdown.is_set():
            _touch_heartbeat()
            config.reload(conn)
            prune_history(conn, config.HISTORY_RETENTION_DAYS)
            playlists = list_playlists(conn, only_enabled=True)
            if sp is None:
                # No Spotify credentials. Idle rather than spin: syncing is
                # impossible and attempting downloads would burn attempts and
                # suspend tracks for two days over a config error.
                logging.error(
                    "Spotify client unavailable; skipping cycle. Set "
                    "SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET and restart."
                )
                playlists = []
            elif not playlists:
                logging.info(
                    "No enabled playlists; waiting for the UI to add some"
                )
            else:
                logging.info(
                    f"Starting new cycle ({len(playlists)} enabled playlist(s))"
                )
            cycle_started_at = time.time()
            try:
                cycle_totals = _run_cycle(sp, conn, playlists, ntfy_url, ntfy_topic)
            except Exception:
                logging.exception("Cycle failed; backing off and retrying")
                if _shutdown.wait(config.CYCLE_ERROR_BACKOFF_SECONDS):
                    break
                continue
            record_task_run(
                conn, "spotify_sync", started=cycle_started_at,
                duration=time.time() - cycle_started_at,
                result=(
                    f"{len(playlists)} playlist(s): {cycle_totals['tracks_added']} added, "
                    f"{cycle_totals['tracks_downloaded']} downloaded, "
                    f"{cycle_totals['tracks_failed']} failed"
                ),
            )
            # Only record cycles that did real work — empty-playlist ticks
            # would dilute the dashboard view of recent activity.
            if playlists:
                record_cycle(
                    conn,
                    started_at=cycle_started_at,
                    duration_seconds=time.time() - cycle_started_at,
                    playlists_synced=len(playlists),
                    tracks_added=cycle_totals["tracks_added"],
                    tracks_downloaded=cycle_totals["tracks_downloaded"],
                    tracks_failed=cycle_totals["tracks_failed"],
                )
            # Sleep until the next cycle, but allow the webui (or a signal
            # handler) to interrupt us via _wake_event.set(). Clearing
            # *after* the wait means a wake_now() that arrives during the
            # cycle is preserved and shortcuts the next sleep.
            runtime_state.set_activity(
                "idle",
                detail=f"Sleeping up to {config.CYCLE_INTERVAL_SECONDS}s until next cycle",
            )
            if _wake_event.wait(config.CYCLE_INTERVAL_SECONDS):
                _wake_event.clear()
                if _shutdown.is_set():
                    break
                logging.info("Cycle interrupted by wake request; starting next cycle")
    finally:
        runtime_state.set_activity("shutting_down", detail="Closing connections")
        logging.info("Shutting down, closing database connection")
        try:
            conn.close()
        except Exception:
            logging.debug("Error closing DB connection", exc_info=True)
