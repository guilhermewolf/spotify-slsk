"""SQLite connection, pragmas and schema migrations (PRAGMA user_version)."""
import logging
import sqlite3

SCHEMA_VERSION = 6


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
            _migrate_track_columns(conn)
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


def _migrate_track_columns(conn: sqlite3.Connection) -> None:
    """v5 and v6: per-playlist tables gain `duration_ms` (v5, Spotify track
    length) and `isrc` (v6, Spotify external_ids.isrc).

    Every catalogued table is migrated here rather than lazily in
    create_table, because the webui reads tables of playlists the daemon
    hasn't synced since the upgrade (disabled ones, for instance). Stored
    snapshots are cleared so the next sync re-reads every track and
    backfills the new columns; existing rows are NULL until then.
    """
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT m.table_name FROM playlists_meta m "
            "JOIN sqlite_master s ON s.type = 'table' AND s.name = m.table_name"
        )
    ]
    with conn:
        for table_name in tables:
            _add_track_columns(conn, table_name)
        conn.execute("UPDATE playlists_meta SET snapshot_id = NULL")


# Columns added to per-playlist tables after their first release.
_TRACK_COLUMNS = (("duration_ms", "INTEGER"), ("isrc", "TEXT"))


def _add_track_columns(conn: sqlite3.Connection, table_name: str) -> None:
    cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{table_name}")')}
    for name, sql_type in _TRACK_COLUMNS:
        if name not in cols:
            conn.execute(f'ALTER TABLE "{table_name}" ADD COLUMN {name} {sql_type}')
            logging.info(f"Migrated {table_name}: added {name} column")


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


def _ensure_blocklist_table(conn: sqlite3.Connection) -> None:
    """Peer files (or whole peers, filename NULL) never to be grabbed again.

    Unlike the per-playlist `_tried` table, this is global and survives a
    successful download — a peer that serves broken transfers stays out.
    Added in schema v5.
    """
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS blocklist ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "username TEXT NOT NULL, "
                "filename TEXT, "
                "size INTEGER, "
                "reason TEXT, "
                "track_id TEXT, "
                "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_blocklist_user_file "
                "ON blocklist (username, filename)"
            )
    except sqlite3.Error as e:
        logging.error(f"Error ensuring blocklist: {e}")


def _ensure_history_table(conn: sqlite3.Connection) -> None:
    """Persistent event log (schema v5): grabbed, imported, download_failed,
    import_rejected, upgraded, retried, deleted_file. Pruned by age via
    prune_history."""
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "event TEXT NOT NULL, "
                "playlist_id TEXT, "
                "track_id TEXT, "
                "data_json TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_track ON history (track_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_ts ON history (ts)")
    except sqlite3.Error as e:
        logging.error(f"Error ensuring history: {e}")


def _ensure_file_checks_table(conn: sqlite3.Connection) -> None:
    """Validation verdicts per library file (schema v6), keyed like the
    per-playlist rows they describe. Written by the importer (lossless
    verdict) and the library health scan; `ignored` is the operator's
    "false positive" mark. `size`/`mtime`/`rules_version` decide whether
    the scan must look at a file again."""
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS file_checks ("
                "table_name TEXT NOT NULL, "
                "track_id TEXT NOT NULL, "
                "path TEXT, "
                "size INTEGER, "
                "mtime REAL, "
                "rules_version INTEGER NOT NULL DEFAULT 0, "
                "audio_check TEXT, "
                "audio_cutoff_hz INTEGER, "
                "audio_drop_db REAL, "
                "problems TEXT NOT NULL DEFAULT '', "
                "evidence_json TEXT, "
                "ignored INTEGER NOT NULL DEFAULT 0, "
                "checked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (table_name, track_id))"
            )
    except sqlite3.Error as e:
        logging.error(f"Error ensuring file_checks: {e}")


def create_connection(db_file: str) -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(db_file, timeout=30)
        _apply_pragmas(conn)
        _ensure_playlists_meta_table(conn)
        _ensure_settings_table(conn)
        _ensure_cycle_history_table(conn)
        _ensure_blocklist_table(conn)
        _ensure_history_table(conn)
        _ensure_file_checks_table(conn)
        _ensure_schema_version(conn)

        # sqlite3.connect() succeeds on a corrupt or non-SQLite file — the
        # error only surfaces on first use, and every helper above logs and
        # swallows sqlite3.Error. Without this probe we would hand back a
        # connection on which nothing works: the webui (which only checks for
        # None) would render empty pages instead of returning 503, and the
        # daemon would run cycles that silently do nothing.
        conn.execute("SELECT 1 FROM playlists_meta LIMIT 1").fetchone()

        # DEBUG: the web UI opens a connection per request.
        logging.debug(f"Connected to SQLite database: {db_file}")
        return conn
    except sqlite3.Error as e:
        logging.error(f"Error connecting to SQLite: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return None
