# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

Production/dev runs via Docker Compose:

```bash
docker compose up --build
```

The stack brings up both the daemon and a colocated `slskd` instance, plus the web UI on `http://127.0.0.1:8000` (daemon port; loopback-bound).

Local (non-Docker) run requires a `.env` sourced into the shell and a reachable `slskd`:

```bash
pip install -r requirements.txt
python app.py
```

## Tests and CI

- `pytest -q` — 99 unit tests covering matchers, search pipeline, DB schema, and webui routes. Requires `pip install -r requirements-dev.txt`.
- `ruff check --select=E,F,W --ignore=E501 .` — mirrors what CI runs.
- `python -m compileall -q .` — cheapest syntax-check.
- `.github/workflows/ci.yaml` runs all three on every push and PR.
- `.github/workflows/release.yaml` builds and pushes the multi-arch image. Tag matrix:
    - Push to `main` → `:main`, `:sha-<short>`, `:latest`.
    - Push a git tag `vX.Y.Z` → `:vX.Y.Z`, `:X.Y.Z`, `:X.Y`, `:X`, `:sha-<short>` (no `:latest` bump — that's intentional so prod deployments pinned to `:latest` follow `main`, not the most recent tag).
    - `workflow_dispatch` on any branch → the branch's normal tag set.
  To release:
  ```bash
  git tag v1.0.0
  git push origin v1.0.0
  ```

`test.py` at the repo root is a gitignored scratchpad.

## Architecture

Single long-running process (`app.py::main`) with two threads:

1. **Daemon thread** (main) — loops over enabled playlists: fetch/diff from Spotify, download pending tracks, tag, move. Interruptible by SIGTERM via `_shutdown` Event.
2. **Web UI thread** — Flask app from `webui/` serving the dashboard. Each request opens its own SQLite connection; WAL mode makes concurrent reads alongside the daemon's writer safe.

Daemon cycle:

1. Install SIGTERM/SIGINT handlers → `_shutdown` threading.Event.
2. Wait for slskd to be healthy (blocking, ~90s at boot).
3. First boot: if `playlists_meta` is empty and `SPOTIFY_PLAYLIST_URLS` env is set, import the URLs into the DB (each fetched once via Spotify API for name + snapshot).
4. Start the web UI thread if `UI_ENABLED=1`.
5. One-shot on-disk reconciliation: walks `SLSKD_PLAYLISTS_DIR/<table>/`, matches existing files to DB rows, marks them downloaded — prevents re-downloading.
6. Enter the cycle loop. Each iteration: `_touch_heartbeat()` → `_reload_settings()` (pulls live tunables from the DB) → iterate `list_playlists(only_enabled=True)` → process. On any unhandled exception, log and back off `CYCLE_ERROR_BACKOFF_SECONDS`. Between cycles, `_shutdown.wait(CYCLE_INTERVAL_SECONDS)` — interruptible.
7. On shutdown, close the SQLite connection in a `finally`.

Docker `HEALTHCHECK` compares `HEARTBEAT_FILE` mtime against `HEARTBEAT_STALE_SECONDS` (default 900s) to detect wedged-but-not-crashed states.

### Module responsibilities

- **`app.py`** — orchestrator. Spotify client setup, Spotify→DB env migration, startup reconciliation, cycle loop, metadata tagging via `mutagen`, file moves, ntfy. Owns the **local-file ↔ DB** matcher (`difflib`-based). Single canonical set of `_`-prefixed helpers.
- **`soulseek_api.py`** — `get_client()` lazy slskd factory, search waterfall (multi-query + CJK passthrough + early-stop on response count), candidate filtering (format allowlist + reported-bitrate floor + effective-bitrate floor via `_effective_mp3_kbps` + version-gate via `_version_mismatch`), sort (format > bitrate > peer upload speed), cleanup via `searches.delete` in `finally`. `refresh_from_db(conn)` reloads tunables at each cycle start; `_interruptible_sleep` honors the shared shutdown Event. Uses `rapidfuzz` for token-set scoring. The two matchers (here vs in `app.py`) serve different phases — don't unify without understanding both call sites.
- **`db.py`** — SQLite persistence. WAL mode + `busy_timeout=5000` + `PRAGMA user_version` migrations. Per-playlist dynamic tables (`pl_<sanitized_name>` + `pl_<…>_tried`) plus a global `playlists_meta` catalogue keyed by Spotify id (with `snapshot_id`, `enabled`, `added_at`) and a global `settings` key-value bag for UI-editable tunables. `get_setting(conn, key, default)` prefers DB, falls back to env, then default — so any env var becomes a mutable setting for free.
- **`webui/`** — Flask dashboard (see next section).
- **`utils.py`** — `sanitize_table_name`, `get_playlist_id` (shared by app and webui).
- **`log_config.py`** — timezone-aware logging; `LIB_LOGLEVEL` (default WARNING) silences noisy third-party loggers (urllib3, spotipy, requests) independently of app `LOGLEVEL`. Installs a `RingBufferHandler` from `runtime_state` alongside the stdout handler so the web UI can tail logs.
- **`runtime_state.py`** — in-process state shared between the daemon thread and the web UI: thread-safe activity tracker (current phase + playlist + track) and a bounded log ring buffer (`LOG_BUFFER_MAX=2000`). Daemon writes; UI reads.

### Web UI

Flask + Jinja2 + Pico.css (classless CDN) + htmx (15s dashboard polling).

Routes:
- `GET /` — dashboard with progress bars + per-playlist counts
- `GET /playlist/<id>` — track list with status filter (all / downloaded / pending / retrying / suspended)
- `GET /track/<pid>/<tid>` — per-track history (path, attempts, rejected filenames)
- `GET /settings` — form for UI-editable tunables; blank fields fall back to env/default
- `GET /healthz` — JSON health (schema version, heartbeat age)
- `GET /logs` — live log tail (polls `/logs.json` every 2s)
- `GET /logs.json?since=<seq>&limit=<n>` — incremental log delta from `runtime_state`
- `GET /activity.json` — current daemon activity (phase / playlist / track)
- `POST /playlists` — add a playlist by Spotify URL (validates via the shared daemon Spotify client)
- `POST /playlist/<id>/toggle` — enable/disable
- `POST /playlist/<id>/delete` — drop from catalogue + drop `pl_*` tables; files kept on disk
- `POST /playlist/<id>/refresh` — clear `snapshot_id` so next cycle re-fetches
- `POST /track/<pid>/<tid>/retry` — clear attempts, suspension, and tried-file history

### State machine per track

Tracks in the per-playlist table have `downloaded`, `attempts`, `suspended_until`, `path`. Flow:

1. New Spotify track → row inserted with `downloaded=0`.
2. `startup_check` (one-shot) matches against files already on disk.
3. `get_pending_tracks` returns rows where `downloaded=0` AND suspension has elapsed.
4. On download failure, `update_download_status(success=False)` increments `attempts`; after reaching `MAX_ATTEMPTS_BEFORE_SUSPEND` (2) the row is suspended for 2 days.
5. On verified success, `clear_tried_entries` wipes the retry history.

Files enter via the shared `/downloads` volume, then move to `/playlists/<table>/` after tagging.

### Environment knobs

Deploy-time / infra (env only):
- `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, `SLSKD_API_KEY`, `NTFY_URL`, `NTFY_TOPIC`
- `SLSKD_HOST_URL`, `SLSKD_URL_BASE`, `SLSKD_DOWNLOADS_DIR`, `SLSKD_PLAYLISTS_DIR`
- `CYCLE_INTERVAL_SECONDS` (300), `CYCLE_ERROR_BACKOFF_SECONDS` (60)
- `HEARTBEAT_FILE`, `HEARTBEAT_STALE_SECONDS`
- `LOGLEVEL` (app), `LIB_LOGLEVEL` (third-party), `TIMEZONE`
- `PUID`/`PGID` (container UID/GID, defaults 1000:1000)
- `UI_ENABLED` (1), `UI_BIND_ADDR` (0.0.0.0), `UI_PORT` (8000), `UI_SECRET_KEY`
- `SPOTIFY_PLAYLIST_URLS` — read **only on first boot** to seed the DB; after that, the UI manages playlists

UI-editable (env is a default; DB override wins):
- `MIN_MATCH_SCORE` (0.62) — `process_downloaded_file` accept threshold
- `SLSKD_PREFERRED_FORMATS` — ordered allowlist, e.g. `flac,mp3,aiff,wav`
- `SLSKD_MIN_PEER_UPLOAD_SPEED` (0) — passed to slskd
- `SLSKD_MIN_EFFECTIVE_MP3_KBPS` (280) — rejects size/duration-inferred sub-280 MP3s
- `SLSKD_EARLY_STOP_RESPONSES` (20) — short-circuit search once this many peers reply
- `SLSKD_MAX_RETRIES` (2), `SLSKD_WAIT_TIMEOUT` (60)

### Gotchas

- **Table names come from Spotify playlist titles.** `sanitize_table_name` is lossy-but-stable; changing its output orphans existing rows. Colliding sanitized names share a table.
- **Matchers are not unified by design.** rapidfuzz in `soulseek_api.py` (search results) vs difflib in `app.py` (local files / DB) serve different phases.
- **`startup_check` passes `destructive=False`** to avoid deleting files on mismatch — regular downloads *do* delete on mismatch.
- **`slskd_api==0.1.5`** is pinned. Transfer-state substrings (`"completed, succeeded"`, `"failed"`) are matched; bumping may break `wait_for_completion`.
- **SQLite WAL mode** means the DB file is always accompanied by `-wal` and `-shm` files. Backups must capture all three or run `PRAGMA wal_checkpoint(TRUNCATE)` first.
- **Web UI has no auth by default.** Compose binds to loopback; if you expose to LAN, put a reverse proxy with auth in front.
- **Schema version is 3.** Upgrading from an older DB is automatic — the ALTER TABLE guards in `_ensure_playlists_meta_table` handle v2 DBs, and `CREATE TABLE IF NOT EXISTS` handles settings.
