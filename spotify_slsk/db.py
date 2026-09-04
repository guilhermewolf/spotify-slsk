import os
import sqlite3
import logging
from .models import Track

SCHEMA_VERSION = 4


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply reliability-oriented pragmas to a fresh connection.

    WAL avoids writers blocking readers; busy_timeout lets SQLite wait for a
    lock instead of raising OperationalError immediately.
    """
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.Error as e:
        logging.warning(f"Failed to apply sqlite pragmas: {e}")


def _ensure_schema_version(conn: sqlite3.Connection) -> None:
    """Bootstrap PRAGMA user_version so future migrations can diff cleanly."""
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            if current == 0:
                logging.info(f"Initialized schema user_version to {SCHEMA_VERSION}")
            else:
                logging.info(
                    f"Migrated schema user_version {current} -> {SCHEMA_VERSION}"
                )
        elif current > SCHEMA_VERSION:
            logging.info(
                f"DB user_version is {current}; code expects {SCHEMA_VERSION}"
            )
    except sqlite3.Error as e:
        logging.warning(f"Failed to read/set schema user_version: {e}")


def _ensure_playlists_meta_table(conn: sqlite3.Connection) -> None:
    """Catalogue table keyed by Spotify playlist id — source of truth for
    which playlists the daemon syncs. `enabled` and `added_at` arrived in
    schema v3; the ALTER TABLE guards handle v2 DBs on upgrade.
    """
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS playlists_meta ("
                "playlist_id TEXT PRIMARY KEY, "
                "table_name TEXT NOT NULL, "
                "name TEXT NOT NULL, "
                "snapshot_id TEXT, "
                "last_synced TIMESTAMP, "
                "enabled INTEGER NOT NULL DEFAULT 1, "
                "added_at TIMESTAMP)"
            )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(playlists_meta)")}
        with conn:
            if "enabled" not in cols:
                conn.execute(
                    "ALTER TABLE playlists_meta ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
                )
            if "added_at" not in cols:
                conn.execute("ALTER TABLE playlists_meta ADD COLUMN added_at TIMESTAMP")
    except sqlite3.Error as e:
        logging.error(f"Error ensuring playlists_meta: {e}")


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    """Key-value bag for UI-editable tunables. DB value overrides env default."""
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "key TEXT PRIMARY KEY, "
                "value TEXT, "
                "updated_at TIMESTAMP)"
            )
    except sqlite3.Error as e:
        logging.error(f"Error ensuring settings: {e}")


def _ensure_cycle_history_table(conn: sqlite3.Connection) -> None:
    """One row per completed daemon cycle (added in schema v4).

    Survives restarts so the dashboard can show trends across deploys —
    that's the only reason this lives in the DB instead of runtime_state.
    """
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cycle_history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "started_at TIMESTAMP NOT NULL, "
                "duration_seconds REAL NOT NULL, "
                "playlists_synced INTEGER NOT NULL DEFAULT 0, "
                "tracks_added INTEGER NOT NULL DEFAULT 0, "
                "tracks_downloaded INTEGER NOT NULL DEFAULT 0, "
                "tracks_failed INTEGER NOT NULL DEFAULT 0)"
            )
    except sqlite3.Error as e:
        logging.error(f"Error ensuring cycle_history: {e}")


def create_connection(db_file):
    try:
        conn = sqlite3.connect(db_file, timeout=30)
        _apply_pragmas(conn)
        _ensure_playlists_meta_table(conn)
        _ensure_settings_table(conn)
        _ensure_cycle_history_table(conn)
        _ensure_schema_version(conn)

        # sqlite3.connect() succeeds on a corrupt or non-SQLite file — the
        # error only surfaces on first use, and every helper above logs and
        # swallows sqlite3.Error. Without this probe we would hand back a
        # connection on which nothing works: the webui (which only checks for
        # None) would render empty pages instead of returning 503, and the
        # daemon would run cycles that silently do nothing.
        conn.execute("SELECT 1 FROM playlists_meta LIMIT 1").fetchone()

        logging.info(f"Connected to SQLite database: {db_file}")
        return conn
    except sqlite3.Error as e:
        logging.error(f"Error connecting to SQLite: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return None


def get_playlist_meta(conn, playlist_id):
    """Return (snapshot_id, table_name, name, last_synced) or a 4-tuple of None."""
    try:
        row = conn.execute(
            "SELECT snapshot_id, table_name, name, last_synced "
            "FROM playlists_meta WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        return row if row else (None, None, None, None)
    except sqlite3.Error as e:
        logging.error(f"Error reading playlists_meta for {playlist_id}: {e}")
        return (None, None, None, None)


def upsert_playlist_meta(conn, playlist_id, table_name, name, snapshot_id):
    """Insert a playlist or update its syncable fields.

    `added_at` is set only on first insert (via COALESCE below); subsequent
    calls don't bump it so the dashboard can sort by insertion order.
    `enabled` is left alone on update — the UI toggles it explicitly.
    """
    try:
        with conn:
            conn.execute(
                "INSERT INTO playlists_meta "
                "(playlist_id, table_name, name, snapshot_id, last_synced, "
                "enabled, added_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP) "
                "ON CONFLICT(playlist_id) DO UPDATE SET "
                "table_name = excluded.table_name, "
                "name = excluded.name, "
                "snapshot_id = excluded.snapshot_id, "
                "last_synced = excluded.last_synced",
                (playlist_id, table_name, name, snapshot_id),
            )
    except sqlite3.Error as e:
        logging.error(f"Error upserting playlists_meta for {playlist_id}: {e}")


def list_playlists(conn, only_enabled: bool = False):
    """Return a list of dicts describing every known playlist."""
    try:
        sql = (
            "SELECT playlist_id, name, table_name, snapshot_id, last_synced, "
            "enabled, added_at FROM playlists_meta"
        )
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY added_at DESC NULLS LAST, name COLLATE NOCASE"
        rows = conn.execute(sql).fetchall()
        return [
            {
                "playlist_id": r[0],
                "name": r[1],
                "table_name": r[2],
                "snapshot_id": r[3],
                "last_synced": r[4],
                "enabled": bool(r[5]) if r[5] is not None else True,
                "added_at": r[6],
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logging.error(f"Could not list playlists: {e}")
        return []


def set_playlist_enabled(conn, playlist_id: str, enabled: bool) -> None:
    try:
        with conn:
            conn.execute(
                "UPDATE playlists_meta SET enabled = ? WHERE playlist_id = ?",
                (1 if enabled else 0, playlist_id),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not toggle playlist {playlist_id}: {e}")


def remove_playlist(conn, playlist_id: str) -> bool:
    """Drop the playlist from the catalogue AND drop its per-playlist tables.

    Returns True if a row was removed. Leaves files on disk — removing them
    is the caller's choice (via the UI dialog) to avoid accidental mass
    deletion.
    """
    try:
        row = conn.execute(
            "SELECT table_name FROM playlists_meta WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        if not row:
            return False
        table_name = row[0]
        with conn:
            conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
            conn.execute(f'DROP TABLE IF EXISTS "{table_name}_tried"')
            conn.execute(
                "DELETE FROM playlists_meta WHERE playlist_id = ?",
                (playlist_id,),
            )
        logging.info(f"Removed playlist {playlist_id} (dropped table {table_name})")
        return True
    except sqlite3.Error as e:
        logging.error(f"Could not remove playlist {playlist_id}: {e}")
        return False


def get_setting(conn, key: str, default=None):
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


def set_setting(conn, key: str, value) -> None:
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


def delete_setting(conn, key: str) -> None:
    """Drop the DB override so env/code default takes over again."""
    try:
        with conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    except sqlite3.Error as e:
        logging.error(f"Could not delete setting {key}: {e}")


def list_settings(conn) -> dict:
    """Return all DB-stored settings as a plain dict."""
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {k: v for k, v in rows}
    except sqlite3.Error as e:
        logging.warning(f"Could not list settings: {e}")
        return {}


def record_cycle(
    conn,
    *,
    started_at: float,
    duration_seconds: float,
    playlists_synced: int,
    tracks_added: int,
    tracks_downloaded: int,
    tracks_failed: int,
    keep_last: int = 200,
) -> None:
    """Persist one cycle's stats and prune anything older than ``keep_last``.

    Capping in the same transaction keeps the table from growing without
    bound on long-running deployments.
    """
    try:
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()
        with conn:
            conn.execute(
                "INSERT INTO cycle_history "
                "(started_at, duration_seconds, playlists_synced, "
                "tracks_added, tracks_downloaded, tracks_failed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    duration_seconds,
                    playlists_synced,
                    tracks_added,
                    tracks_downloaded,
                    tracks_failed,
                ),
            )
            conn.execute(
                "DELETE FROM cycle_history WHERE id NOT IN ("
                "SELECT id FROM cycle_history ORDER BY id DESC LIMIT ?)",
                (keep_last,),
            )
    except sqlite3.Error as e:
        logging.error(f"Error recording cycle history: {e}")


def list_cycles(conn, limit: int = 20) -> list:
    """Return the most recent cycles, newest first."""
    try:
        rows = conn.execute(
            "SELECT started_at, duration_seconds, playlists_synced, "
            "tracks_added, tracks_downloaded, tracks_failed "
            "FROM cycle_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "started_at": r[0],
                "duration_seconds": r[1],
                "playlists_synced": r[2],
                "tracks_added": r[3],
                "tracks_downloaded": r[4],
                "tracks_failed": r[5],
            }
            for r in rows
        ]
    except sqlite3.Error as e:
        logging.warning(f"Could not list cycle history: {e}")
        return []


def playlist_stats(conn, table_name: str) -> dict:
    """Return counts the dashboard needs for one playlist.

    Missing tables (deleted playlist still listed in an older in-flight page)
    yield zeros rather than an exception.
    """
    try:
        row = conn.execute(
            f'SELECT '
            f'  COUNT(*), '
            f'  COALESCE(SUM(CASE WHEN downloaded = 1 THEN 1 ELSE 0 END), 0), '
            f'  COALESCE(SUM(CASE WHEN downloaded = 0 AND '
            f'    (suspended_until IS NULL OR suspended_until < CURRENT_TIMESTAMP) '
            f'    THEN 1 ELSE 0 END), 0), '
            f'  COALESCE(SUM(CASE WHEN downloaded = 0 AND suspended_until IS NOT NULL '
            f'    AND suspended_until >= CURRENT_TIMESTAMP THEN 1 ELSE 0 END), 0), '
            # "retrying" is a subset of "pending": not downloaded, not
            # currently suspended, but has already failed at least once. The
            # dashboard shows it separately so a user can tell "not tried yet"
            # from "tried and failed".
            f'  COALESCE(SUM(CASE WHEN downloaded = 0 AND attempts > 0 AND '
            f'    (suspended_until IS NULL OR suspended_until < CURRENT_TIMESTAMP) '
            f'    THEN 1 ELSE 0 END), 0) '
            f'FROM "{table_name}"'
        ).fetchone()
        return {
            "total": row[0],
            "downloaded": row[1],
            "pending": row[2],
            "suspended": row[3],
            "retrying": row[4],
        }
    except sqlite3.Error as e:
        logging.warning(f"Could not get stats for {table_name}: {e}")
        return {
            "total": 0,
            "downloaded": 0,
            "pending": 0,
            "suspended": 0,
            "retrying": 0,
        }


def _track_status(downloaded, attempts, suspended_until, now):
    if downloaded:
        return "downloaded"
    if suspended_until and suspended_until >= now:
        return "suspended"
    if attempts and attempts > 0:
        return "retrying"
    return "pending"


def list_tracks(conn, table_name: str) -> list:
    """Return one dict per track in the playlist, with a derived status."""
    try:
        rows = conn.execute(
            f'SELECT id, name, artists, album, downloaded, path, attempts, '
            f'last_attempt, suspended_until, CURRENT_TIMESTAMP '
            f'FROM "{table_name}" ORDER BY name COLLATE NOCASE'
        ).fetchall()
        result = []
        for r in rows:
            d = {
                "id": r[0],
                "name": r[1],
                "artists": r[2],
                "album": r[3],
                "downloaded": bool(r[4]),
                "path": r[5],
                "attempts": r[6] or 0,
                "last_attempt": r[7],
                "suspended_until": r[8],
            }
            d["status"] = _track_status(d["downloaded"], d["attempts"], r[8], r[9])
            result.append(d)
        return result
    except sqlite3.Error as e:
        logging.error(f"Could not list tracks for {table_name}: {e}")
        return []


def get_track(conn, table_name: str, track_id: str):
    """Return one track dict or None."""
    try:
        r = conn.execute(
            f'SELECT id, name, artists, album, downloaded, path, attempts, '
            f'last_attempt, suspended_until, CURRENT_TIMESTAMP '
            f'FROM "{table_name}" WHERE id = ?',
            (track_id,),
        ).fetchone()
        if not r:
            return None
        d = {
            "id": r[0],
            "name": r[1],
            "artists": r[2],
            "album": r[3],
            "downloaded": bool(r[4]),
            "path": r[5],
            "attempts": r[6] or 0,
            "last_attempt": r[7],
            "suspended_until": r[8],
        }
        d["status"] = _track_status(d["downloaded"], d["attempts"], r[8], r[9])
        return d
    except sqlite3.Error as e:
        logging.error(f"Could not read track {track_id} from {table_name}: {e}")
        return None


def retry_track(conn, table_name: str, track_id: str) -> None:
    """Clear suspended_until + attempts and wipe the tried-files history so
    the track is picked up fresh on the next cycle."""
    try:
        with conn:
            conn.execute(
                f'UPDATE "{table_name}" SET suspended_until = NULL, attempts = 0 '
                f'WHERE id = ?',
                (track_id,),
            )
            conn.execute(
                f'DELETE FROM "{table_name}_tried" WHERE track_id = ?',
                (track_id,),
            )
    except sqlite3.Error as e:
        logging.error(f"Could not retry track {track_id} in {table_name}: {e}")

def create_table(conn, playlist_name):
    """Create a table dynamically based on the sanitized playlist name"""
    table_name = playlist_name
    try:
        sql_create_tracks_table = f"""CREATE TABLE IF NOT EXISTS {table_name} (
                                        id TEXT PRIMARY KEY,
                                        name TEXT NOT NULL,
                                        artists TEXT NOT NULL,
                                        album TEXT NOT NULL,
                                        downloaded INTEGER DEFAULT 0,
                                        path TEXT,
                                        attempts INTEGER DEFAULT 0,
                                        last_attempt TIMESTAMP,
                                        suspended_until TIMESTAMP,
                                        last_upgrade_check TIMESTAMP,
                                        tried_files TEXT DEFAULT ''
                                    );"""
        cursor = conn.cursor()
        cursor.execute(sql_create_tracks_table)
        logging.info(f"Table {table_name} created or already exists.")
        # Migrate older per-playlist tables that pre-date the upgrade-pass column.
        cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()}
        if "last_upgrade_check" not in cols:
            with conn:
                conn.execute(
                    f'ALTER TABLE "{table_name}" ADD COLUMN last_upgrade_check TIMESTAMP'
                )
            logging.info(f"Migrated {table_name}: added last_upgrade_check column")
        create_tried_table(conn, playlist_name)
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_id ON {table_name} (id);")
        logging.info(f"Index on {table_name}(id) created or already exists.")
    except sqlite3.Error as e:
        logging.error(f"Error creating table {table_name}: {e}")

def insert_track(conn, playlist_name, track):
    table_name = playlist_name
    sql = f'''INSERT OR IGNORE INTO {table_name}(id, name, artists, album) VALUES (?, ?, ?, ?)'''
    try:
        with conn:
            conn.execute(sql, track)
        logging.info(f"Inserted track into {table_name}: {track[1]} by {track[2]}")
    except sqlite3.Error as e:
        logging.error(f"Error inserting track into {table_name}: {e}")

def fetch_all_tracks(conn, playlist_name):
    table_name = playlist_name
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT id, name, artists, album FROM {table_name}")
        return cursor.fetchall()
    except sqlite3.Error as e:
        logging.error(f"Error fetching tracks from {table_name}: {e}")
        return []

MAX_ATTEMPTS_BEFORE_SUSPEND = 2


def update_download_status(conn, track_id, table_name, success=False, file_path=None):
    try:
        with conn:
            cursor = conn.cursor()
            if success:
                cursor.execute(
                    f'UPDATE "{table_name}" SET downloaded = 1, attempts = 0, '
                    f'suspended_until = NULL, path = ?, last_attempt = CURRENT_TIMESTAMP '
                    f'WHERE id = ?',
                    (file_path, track_id),
                )
                logging.info(f"Marked track {track_id} downloaded: {file_path}")
            else:
                cursor.execute(
                    f'UPDATE "{table_name}" SET attempts = attempts + 1, '
                    f'last_attempt = CURRENT_TIMESTAMP WHERE id = ?',
                    (track_id,),
                )
                cursor.execute(
                    f'SELECT attempts FROM "{table_name}" WHERE id = ?',
                    (track_id,),
                )
                row = cursor.fetchone()
                attempts = row[0] if row else 0
                if attempts >= MAX_ATTEMPTS_BEFORE_SUSPEND:
                    cursor.execute(
                        f'UPDATE "{table_name}" SET suspended_until = datetime("now", "+2 days") '
                        f'WHERE id = ?',
                        (track_id,),
                    )
                    logging.info(
                        f"Track {track_id} hit {attempts} attempts, suspended for 2 days"
                    )
                else:
                    logging.info(f"Track {track_id} attempts now {attempts}")
    except sqlite3.Error as e:
        logging.error(f"Error updating track status for {track_id} in {table_name}: {e}")

def create_tried_table(conn, playlist_name):
    tried_table_name = f"{playlist_name}_tried"
    try:
        with conn:
            conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{tried_table_name}" ('
                f'track_id TEXT NOT NULL, '
                f'file_path TEXT NOT NULL, '
                f'PRIMARY KEY (track_id, file_path))'
            )
        logging.info(f"Tried table {tried_table_name} created or already exists.")
    except sqlite3.Error as e:
        logging.error(f"Error creating tried table {tried_table_name}: {e}")

def get_tried_files(conn, table_name, track_id):
    tried_table_name = f"{table_name}_tried"
    cursor = conn.cursor()
    cursor.execute(
        f'SELECT file_path FROM "{tried_table_name}" WHERE track_id = ?',
        (track_id,)
    )
    rows = cursor.fetchall()
    return [row[0] for row in rows] if rows else []


def add_tried_file(conn, table_name, track_id, file_path):
    tried_table_name = f"{table_name}_tried"
    try:
        with conn:
            conn.execute(
                f'INSERT OR IGNORE INTO "{tried_table_name}" (track_id, file_path) VALUES (?, ?)',
                (track_id, file_path),
            )
        logging.info(f"Added attempted file for {track_id}: {file_path}")
    except sqlite3.Error as e:
        logging.error(f"Failed to insert into {tried_table_name} for {track_id}: {e}")


def clear_tried_entries(conn, playlist_name, track_id):
    """Wipe the rejected-filename history for one track.

    The history lives in the `_tried` companion table — the same place
    get_tried_files reads and add_tried_file writes. This used to blank a
    `tried_files` column on the main table instead, which nothing reads, so
    every filename ever rejected for a track stayed blacklisted forever and
    the upgrade pass could never reconsider it.
    """
    try:
        with conn:
            conn.execute(
                f'DELETE FROM "{playlist_name}_tried" WHERE track_id = ?',
                (track_id,),
            )
        logging.info(f"Cleared tried entries for track {track_id} in {playlist_name}")
    except sqlite3.Error as e:
        logging.error(
            f"Failed to clear tried entries for {track_id} in {playlist_name}: {e}"
        )


def get_pending_tracks(conn, playlist_name: str) -> list:
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            SELECT id, name, artists, album
            FROM "{playlist_name}"
            WHERE downloaded = 0
              AND (suspended_until IS NULL OR suspended_until < CURRENT_TIMESTAMP)
        """)
        rows = cursor.fetchall()
        return [Track(row[0], row[1], row[2], row[3], playlist_name) for row in rows]
    except sqlite3.Error as e:
        logging.error(f"Failed to fetch pending tracks from {playlist_name}: {e}")
        return []


def get_upgrade_candidates(conn, playlist_name: str, interval_hours: int) -> list:
    """Return downloaded tracks whose last upgrade-check is stale or absent.

    Throttling lives at the row level so a 200-track library doesn't fire
    200 slskd searches every cycle. Returns (Track, current_path) tuples.
    `interval_hours <= 0` disables the feature entirely.
    """
    if interval_hours <= 0:
        return []
    try:
        cursor = conn.cursor()
        cursor.execute(
            f'SELECT id, name, artists, album, path FROM "{playlist_name}" '
            f'WHERE downloaded = 1 '
            f"AND (last_upgrade_check IS NULL "
            f"     OR last_upgrade_check < datetime('now', ?))",
            (f"-{interval_hours} hours",),
        )
        rows = cursor.fetchall()
        return [
            (Track(r[0], r[1], r[2], r[3], playlist_name), r[4])
            for r in rows
        ]
    except sqlite3.Error as e:
        logging.error(f"Failed to fetch upgrade candidates from {playlist_name}: {e}")
        return []


def mark_upgrade_checked(conn, playlist_name: str, track_id: str) -> None:
    """Stamp last_upgrade_check so the throttle window starts now.

    Called whether or not an upgrade was found — a fruitless check still
    consumed a search, so it counts toward the interval.
    """
    try:
        with conn:
            conn.execute(
                f'UPDATE "{playlist_name}" SET last_upgrade_check = CURRENT_TIMESTAMP '
                f'WHERE id = ?',
                (track_id,),
            )
    except sqlite3.Error as e:
        logging.error(
            f"Failed to update last_upgrade_check for {track_id} in {playlist_name}: {e}"
        )
