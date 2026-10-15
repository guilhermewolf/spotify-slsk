"""SQLite persistence.

Split by table family; every public name is re-exported here so callers
keep using ``from spotify_slsk import db`` / ``db.<name>``.
"""
from .connection import (
    SCHEMA_VERSION,
    create_connection,
)
from .playlists import (
    get_playlist_meta,
    upsert_playlist_meta,
    rename_playlist_meta,
    list_playlists,
    set_playlist_enabled,
    remove_playlist,
)
from .settings import (
    get_setting,
    set_setting,
    delete_setting,
    list_settings,
)
from .cycles import (
    record_cycle,
    list_cycles,
)
from .tracks import (
    playlist_stats,
    list_tracks,
    get_track,
    retry_track,
    create_table,
    insert_track,
    backfill_durations,
    fetch_all_tracks,
    MAX_ATTEMPTS_BEFORE_SUSPEND,
    update_download_status,
    create_tried_table,
    get_tried_files,
    add_tried_file,
    clear_tried_entries,
    get_pending_tracks,
    get_upgrade_candidates,
    mark_upgrade_checked,
)
from .blocklist import (
    add_blocklist,
    list_blocklist,
    remove_blocklist,
    load_blocklist,
    is_blocklisted,
)
from .history import (
    HISTORY_EVENTS,
    FAILURE_EVENTS,
    record_history,
    list_history,
    latest_failures,
    prune_history,
)

__all__ = [
    "SCHEMA_VERSION",
    "create_connection",
    "get_playlist_meta",
    "upsert_playlist_meta",
    "rename_playlist_meta",
    "list_playlists",
    "set_playlist_enabled",
    "remove_playlist",
    "get_setting",
    "set_setting",
    "delete_setting",
    "list_settings",
    "record_cycle",
    "list_cycles",
    "playlist_stats",
    "list_tracks",
    "get_track",
    "retry_track",
    "create_table",
    "insert_track",
    "backfill_durations",
    "fetch_all_tracks",
    "MAX_ATTEMPTS_BEFORE_SUSPEND",
    "update_download_status",
    "create_tried_table",
    "get_tried_files",
    "add_tried_file",
    "clear_tried_entries",
    "get_pending_tracks",
    "get_upgrade_candidates",
    "mark_upgrade_checked",
    "add_blocklist",
    "list_blocklist",
    "remove_blocklist",
    "load_blocklist",
    "is_blocklisted",
    "HISTORY_EVENTS",
    "FAILURE_EVENTS",
    "record_history",
    "list_history",
    "latest_failures",
    "prune_history",
]
