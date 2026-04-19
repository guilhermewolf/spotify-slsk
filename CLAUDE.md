# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

Production/dev runs via Docker Compose, which brings up both the app and a colocated `slskd` instance:

```bash
docker compose up --build
```

Local (non-Docker) run requires a `.env` sourced into the shell and a reachable `slskd` instance:

```bash
pip install -r requirements.txt
python app.py
```

There is no test suite. `test.py` is a manual scratchpad for poking at the `slskd` API with hardcoded credentials — do not treat it as tests and do not wire it into CI.

CI (`.github/workflows/release.yaml`) only builds and pushes the multi-arch Docker image on pushes to `main`; it does not lint or test.

## Architecture

The app is a single long-running process (`app.py::main`) that loops forever over the playlists listed in `SPOTIFY_PLAYLIST_URLS`. One pass per playlist does: fetch from Spotify → reconcile against local disk → download anything still pending → tag → move into the playlist folder. Then `sleep_interval(5)` and repeat.

### Module responsibilities

- **`app.py`** — orchestrator. Spotify client setup, playlist diffing, startup reconciliation, metadata tagging (`mutagen`), file moves, ntfy notifications. Also owns the *local-file ↔ DB* matching logic (`difflib`-based, see `find_closest_db_match`, `_find_best_local_match`, `_looks_like_match`).
- **`soulseek_api.py`** — everything that talks to `slskd`: search, candidate filtering, enqueue, transfer monitoring, post-download verification. Owns the *search-result ↔ expected track* matching logic (`rapidfuzz`-based, see `extract_candidates`). Note these two matchers live in different files because they run at different phases and use different scoring — don't unify them without understanding both call sites.
- **`db.py`** — SQLite persistence. Each playlist gets its **own table** named `pl_<sanitized_playlist_name>` plus a sibling `pl_<…>_tried` table that records filenames we already rejected for a given track (so retries don't redownload the same bad file). Table names are interpolated directly into SQL; `sanitize_table_name` in `utils.py` is the only defense against injection and MUST be used for any new query that names a table dynamically.
- **`models.py`** — `Track` value object passed between layers.
- **`log_config.py`** — timezone-aware logging, driven by `TIMEZONE` and `LOGLEVEL`.

### State machine per track

Tracks live in the per-playlist DB table with columns `downloaded`, `attempts`, `suspended_until`, `path`, plus a `tried_files` JSON column. Flow:

1. New Spotify track → row inserted with `downloaded=0`.
2. `startup_check` tries to match the row against files already on disk under `SLSKD_PLAYLISTS_DIR/<table>/`. A successful local match sets `downloaded=1` and `path=<file>` without touching Soulseek.
3. `get_pending_tracks` returns rows where `downloaded=0` AND `suspended_until` has elapsed.
4. On download failure, `update_download_status(success=False)` increments `attempts`; after 2 attempts the row is suspended for 2 days.
5. On verified success, `clear_tried_entries` wipes the retry history for that track.

Files enter the system via the `/downloads` volume (shared between the `slskd` container and this app), then get moved to `/playlists/<table>/` after tagging.

### Key environment knobs (beyond the README table)

- `SLSKD_PLAYLISTS_DIR` (default `/playlists`) — final home for verified files.
- `SLSKD_DOWNLOADS_DIR` (default `/downloads`) — where `slskd` drops completed downloads; must be a shared volume with the `slskd` service.
- `MIN_MATCH_SCORE` (default `0.62`) — threshold in `process_downloaded_file` for accepting a downloaded file as matching a DB row.
- `SLSKD_WAIT_TIMEOUT` (default `60`) — seconds to wait for a file to appear on disk after `slskd` reports the transfer completed.
- `SLSKD_MAX_RETRIES` (default `2`) — per-track download attempts before giving up on a search.
- `LOGLEVEL`, `TIMEZONE` — consumed by `log_config.setup_logging`.

### Gotchas

- Table names come from Spotify playlist titles, so `sanitize_table_name` must stay lossy-but-stable; changing its output format orphans existing rows.
- `startup_check` and the reconcile path in `process_downloaded_file` pass `reconcile=True`/`destructive=False` to avoid deleting files that don't match — regular downloads delete on mismatch. Preserve this distinction when editing `_reject_and_log`.
- The two matching subsystems (`extract_candidates` for Soulseek results, `_looks_like_match`/`find_closest_db_match` for local files) have overlapping but not identical token-cleaning logic (`_STOP_PHRASES`, bracket stripping, artist splitters). Fixing a mismatch in one does not fix the other.
- `slskd_api==0.1.5` is pinned; its transfer-state strings (`"completed, succeeded"`, `"failed"`, etc.) are matched as substrings in `wait_for_completion` — bumping the library may break that check.
