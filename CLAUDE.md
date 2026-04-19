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

## Tests and CI

- `pytest -q` runs the unit test suite in `tests/`. Requires `pip install -r requirements-dev.txt` for pytest and ruff.
- `ruff check --select=E,F,W --ignore=E501 .` mirrors the lint CI does.
- `python -m compileall -q .` is the cheapest syntax-check.
- `.github/workflows/ci.yaml` runs all three on every push and PR.
- `.github/workflows/release.yaml` builds and pushes the multi-arch image on pushes to `main`.

`test.py` at the repo root is a **manual scratchpad**, not a test — it's gitignored and takes credentials from env.

## Architecture

The app is a single long-running process (`app.py::main`) that loops forever over the playlists listed in `SPOTIFY_PLAYLIST_URLS`. Flow:

1. Install SIGTERM/SIGINT handlers that set a `_shutdown` threading.Event.
2. Wait for slskd to be healthy (blocking, ~90s timeout at boot).
3. **One-shot reconciliation** across all playlists — matches on-disk files against DB rows so the daemon doesn't redownload tracks that are already present. This is the expensive disk walk and runs only once per daemon start.
4. Enter the cycle loop: for each playlist, fetch new Spotify tracks and attempt to download anything still pending. On any unhandled exception, log and back off `CYCLE_ERROR_BACKOFF_SECONDS` (default 60s). Between cycles, `_shutdown.wait(CYCLE_INTERVAL_SECONDS)` (default 300s) — interruptible by SIGTERM so Docker stop unwinds cleanly.
5. On shutdown, close the SQLite connection in a `finally` block.

The loop also touches `HEARTBEAT_FILE` (default `/tmp/heartbeat`) at the top of each iteration. Docker's `HEALTHCHECK` marks the container unhealthy if the heartbeat is older than `HEARTBEAT_STALE_SECONDS` (default 900s) — catches wedged-but-not-crashed states that `restart: unless-stopped` alone doesn't cover.

### Module responsibilities

- **`app.py`** — orchestrator. Spotify client setup, playlist diffing, one-shot startup reconciliation, metadata tagging (`mutagen`), file moves, ntfy notifications. Owns the **local-file ↔ DB** matcher (`difflib`-based, in `_looks_like_match`, `_find_best_local_match`, `score_track_match`, `find_closest_db_match`) — one canonical set of `_`-prefixed helpers (`_tokenize`, `_split_artists`, `_artists_overlap`, `_titles_token_equivalent`, `_remix_equivalent`, `_similar`).
- **`soulseek_api.py`** — slskd client factory (`get_client()` lazy singleton) plus the **search-result ↔ expected track** matcher (`rapidfuzz`-based `extract_candidates`). Also owns query construction (`_build_search_queries` — multi-query waterfall: `title+artist` → album-cleaned → `title` only; CJK titles bypass punctuation-stripping), candidate filtering (format allowlist + reported-bitrate floor + effective-bitrate floor via `_effective_mp3_kbps` + version-gate penalty via `_version_mismatch`), and `sort_candidates` (format > bitrate > peer upload speed). `perform_search` cleans up its slskd search row in a `finally`. The two matchers live in different files because they run at different phases and use different scoring — don't unify them without understanding both call sites.
- **`db.py`** — SQLite persistence with WAL mode, `busy_timeout=5000`, and `PRAGMA user_version` bootstrapping for future migrations. Each playlist gets its **own table** named `pl_<sanitized_playlist_name>` plus a sibling `pl_<…>_tried` table. Table names are interpolated directly into SQL; `sanitize_table_name` in `utils.py` is the only defense against injection — use it for any new query that names a table dynamically. All mutations go through `with conn:` blocks for atomicity.
- **`models.py`** — `Track` value object passed between layers.
- **`log_config.py`** — timezone-aware logging, driven by `TIMEZONE` and `LOGLEVEL`.

### State machine per track

Tracks live in the per-playlist DB table with columns `downloaded`, `attempts`, `suspended_until`, `path`, plus a sibling `<table>_tried` table for rejected filenames. Flow:

1. New Spotify track → row inserted with `downloaded=0`.
2. `startup_check` (one-shot) tries to match the row against files already on disk under `SLSKD_PLAYLISTS_DIR/<table>/`. A successful local match sets `downloaded=1` and `path=<file>` without touching Soulseek.
3. `get_pending_tracks` returns rows where `downloaded=0` AND `suspended_until` has elapsed.
4. On download failure, `update_download_status(success=False)` increments `attempts` first, then suspends the row for 2 days once `attempts >= MAX_ATTEMPTS_BEFORE_SUSPEND` (2).
5. On verified success, `clear_tried_entries` wipes the retry history.

Files enter the system via the `/downloads` volume (shared between the `slskd` container and this app), then move to `/playlists/<table>/` after tagging.

### Environment knobs

Runtime / loop
- `CYCLE_INTERVAL_SECONDS` (300) — between cycles. Interruptible by SIGTERM.
- `CYCLE_ERROR_BACKOFF_SECONDS` (60) — backoff after a cycle raises.
- `HEARTBEAT_FILE` (`/tmp/heartbeat`), `HEARTBEAT_STALE_SECONDS` (900) — healthcheck.

Spotify / playlists
- `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, `SPOTIFY_PLAYLIST_URLS` (comma-separated).

slskd
- `SLSKD_HOST_URL`, `SLSKD_API_KEY`, `SLSKD_URL_BASE`.
- `SLSKD_PLAYLISTS_DIR` (`/playlists`), `SLSKD_DOWNLOADS_DIR` (`/downloads`).
- `SLSKD_PREFERRED_FORMATS` — ordered allowlist, e.g. `flac,mp3,aiff,wav`.
- `SLSKD_WAIT_TIMEOUT` (60), `SLSKD_MAX_RETRIES` (2).
- `SLSKD_MIN_PEER_UPLOAD_SPEED` (0) — passed to slskd's search endpoint.
- `SLSKD_MIN_EFFECTIVE_MP3_KBPS` (280) — rejects MP3s whose size/duration proves they're sub-280 kbps even when tagged 320.

Matching / logging
- `MIN_MATCH_SCORE` (0.62) — `process_downloaded_file` accept threshold.
- `LOGLEVEL`, `TIMEZONE`.

Container
- `PUID`, `PGID` — UID/GID the container runs as (default 1000:1000). Must match the host user that owns `./data`, `./downloads`, `./playlists`.

### Gotchas

- **Table names come from Spotify playlist titles.** `sanitize_table_name` is lossy-but-stable; changing its output format orphans existing rows. Playlists with colliding sanitized names will share a table.
- **Matchers are not unified by design.** `extract_candidates` (rapidfuzz, soulseek_api.py) and `_looks_like_match` / `score_track_match` (difflib, app.py) have overlapping token-cleaning logic but serve different phases. Fixing a false match in one does not fix it in the other.
- **`startup_check` and the reconcile path in `process_downloaded_file`** pass `reconcile=True`/`destructive=False` to avoid deleting files that don't match — regular downloads *do* delete on mismatch. Preserve this distinction when editing `_reject_and_log`.
- **`slskd_api==0.1.5` is pinned.** Its transfer-state strings (`"completed, succeeded"`, `"failed"`, etc.) are matched as substrings in `wait_for_completion` — bumping the library may break that check. It also does not have a `stop(id)` method in older versions, but it does have `delete(id)` which we use in `perform_search`'s `finally`.
- **SQLite WAL mode** means the DB file is always accompanied by `-wal` and `-shm` files. Backups must capture all three or run `PRAGMA wal_checkpoint(TRUNCATE)` first.
