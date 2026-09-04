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
python -m spotify_slsk
```

## Quality gate

`make check` is the single gate, and `.github/workflows/ci.yaml` calls the same
targets — if `make check` passes locally, CI should agree.

```bash
make install                         # runtime + dev dependencies
make check                           # compile + lint + test + audit
make check PYTHON=.venv/bin/python   # against a virtualenv
```

Individual targets: `make compile`, `make lint`, `make test`, `make audit`,
`make docker-build`.

- `make test` — 210 tests. Fast (~1s); no network, no external services.
- `make audit` — `pip-audit` against the pinned `requirements.txt`. Advisories
  fail CI, so dependency CVEs surface on a PR rather than in production.
- CI also builds the image and asserts it runs as uid 1000 and that no
  `.env` / `data/` / `playlists/` / `slsk_app/` path leaked into it.

`test.py` at the repo root is a gitignored scratchpad.

## Test isolation (important)

`tests/conftest.py` makes the suite **incapable of reaching production**, and
this is deliberate — do not weaken it:

- Environment values are **overridden, not defaulted**. The test modules'
  `os.environ.setdefault(...)` calls are no-ops when the developer's shell
  already exports real values; before conftest existed, running `pytest` with a
  sourced `.env` pointed the suite at the real slskd and at the real music
  library — the directory `app._reject_and_log` will `os.remove` from.
- `SLSKD_DOWNLOADS_DIR`, `SLSKD_PLAYLISTS_DIR`, `HEARTBEAT_FILE` and the
  spotipy cache all point into a per-session temp tree.
- Constructing a real `slskd_api.SlskdClient` raises, as does any real outbound
  HTTP request. Tests that need those boundaries mock them explicitly.
- `tests/test_isolation.py` asserts all of the above, so weakening conftest
  fails the suite rather than silently re-pointing it at production.

Use the `sandbox_dirs` fixture for anything touching the filesystem.

## Layout

```
spotify_slsk/          the application package
  __main__.py          entrypoint: python -m spotify_slsk
  app.py               daemon orchestration
  db.py                SQLite persistence
  soulseek_api.py      slskd search/download
  models.py utils.py log_config.py
  webui/               Flask dashboard (templates/, static/)
tests/                 pytest suite (conftest.py enforces isolation)
```

Everything is inside the package, so imports between modules are relative
(`from .db import ...`). The daemon resolves `./data/playlist_tracks.db`
relative to the working directory — run it from the repo root, which is what
the container does (`WORKDIR /app`).

## Architecture

Single long-running process (`spotify_slsk/app.py::main`) with two threads:

1. **Daemon thread** (main) — loops over enabled playlists: fetch/diff from Spotify, download pending tracks, tag, move. Interruptible by SIGTERM via `_shutdown` Event.
2. **Web UI thread** — Flask app from `spotify_slsk/webui/` serving the dashboard. Each request opens its own SQLite connection; WAL mode makes concurrent reads alongside the daemon's writer safe.

Startup order in `main()` matters and is load-bearing:

1. Install SIGTERM/SIGINT handlers → `_shutdown` threading.Event.
2. Open the DB. `create_connection` probes the connection before returning it —
   `sqlite3.connect` succeeds on a corrupt file and every helper swallows
   `sqlite3.Error`, so without the probe callers get a connection on which
   nothing works.
3. Build the Spotify client. Returns `None` rather than raising if credentials
   are missing, so the UI still comes up.
4. First boot: if `playlists_meta` is empty and `SPOTIFY_PLAYLIST_URLS` env is set, import the URLs into the DB.
5. **Start the web UI thread** if `UI_ENABLED=1` — *before* waiting on slskd.
6. `block_until_slskd_healthy()` retries indefinitely, heartbeating while it
   waits. It does not raise: an slskd outage must not take down the dashboard
   the operator needs to diagnose it. Cycles do not start until slskd is
   healthy, because every download would fail and two failures suspend a track
   for two days — turning a brief outage into a self-inflicted backlog.
7. One-shot on-disk reconciliation (`startup_check`) — matches existing files to DB rows so they aren't re-downloaded.
8. Enter the cycle loop: `_touch_heartbeat()` → `_reload_settings()` → iterate `list_playlists(only_enabled=True)`. On any unhandled exception, log and back off `CYCLE_ERROR_BACKOFF_SECONDS`. Between cycles, `_wake_event.wait(CYCLE_INTERVAL_SECONDS)` — interruptible by the UI's `wake_now()`.
9. On shutdown, close the SQLite connection in a `finally`.

Docker `HEALTHCHECK` compares `HEARTBEAT_FILE` mtime against `HEARTBEAT_STALE_SECONDS` (default 900s). The heartbeat is touched **per playlist and per track**, not once per cycle: one track can occupy minutes (60s search + up to 300s transfer + 60s post-processing per candidate), so a coarser touch made a busy daemon indistinguishable from a wedged one.

### Module responsibilities

- **`spotify_slsk/app.py`** — orchestrator. Spotify client setup, Spotify→DB env migration, startup reconciliation, cycle loop, metadata tagging via `mutagen`, file moves, ntfy. Owns the **local-file ↔ DB** matcher (`difflib`-based).
- **`spotify_slsk/soulseek_api.py`** — `get_client()` lazy slskd factory, search waterfall (multi-query + CJK passthrough + early-stop on response count), candidate filtering (format allowlist + reported-bitrate floor + effective-bitrate floor via `_effective_mp3_kbps` + version-gate via `_version_mismatch`), sort (version tier > format > bitrate > peer upload speed), cleanup via `searches.delete` in `finally`. `refresh_from_db(conn)` reloads tunables at each cycle start; `_interruptible_sleep` honors the shared shutdown Event. Uses `rapidfuzz` for token-set scoring.
- **`spotify_slsk/db.py`** — SQLite persistence. WAL mode + `busy_timeout=5000` + `PRAGMA user_version` migrations. Per-playlist dynamic tables (`pl_<sanitized_name>` + `pl_<…>_tried`) plus a global `playlists_meta` catalogue keyed by Spotify id and a global `settings` key-value bag. `get_setting(conn, key, default)` prefers DB, falls back to env, then default — so any env var becomes a mutable setting for free.
- **`spotify_slsk/webui/`** — Flask dashboard (see next section).
- **`spotify_slsk/runtime_state.py`** — in-process observability: thread-safe current-activity dict, a bounded log ring buffer (fed by a handler installed in `log_config`), and per-track slskd search history. All of it is per-process and resets on restart — deliberately not persisted.
- **`spotify_slsk/utils.py`** — `sanitize_table_name`, `get_playlist_id`.
- **`spotify_slsk/log_config.py`** — timezone-aware logging; `LIB_LOGLEVEL` (default WARNING) silences noisy third-party loggers independently of app `LOGLEVEL`.

### Web UI

Flask + Jinja2 + Pico.css + htmx. **Assets are self-hosted** in `spotify_slsk/webui/static/`
(Pico 2.0.6, htmx 1.9.12) — not on a CDN. A compromised CDN could drive the
unauthenticated mutating routes, and self-hosting is what allows the CSP to stay
at `script-src 'self'`. `spotify_slsk/webui/static/app.css` is the small design layer on top
of Pico. Note the classless Pico build ships **no** `.secondary`/`.outline`
classes; `app.css` defines them.

Routes:
- `GET /` — dashboard: library totals + playlist cards (htmx-refreshed every 15s as one swap)
- `GET /playlist/<id>` — track list with status filter + `?q=` search
- `GET /track/<pid>/<tid>` — per-track state, history, rejected filenames
- `GET /settings` — grouped, validated tunables
- `GET /logs` · `GET /logs.json?since=&limit=` · `GET /activity.json` — live log tail and current daemon phase
- `GET /file?rel=` — serves a downloaded file for the in-browser audio preview; `rel` is resolved against the playlists root and re-checked with `commonpath` after symlink normalisation
- `GET /healthz` — JSON health (schema version, heartbeat age, reason)
- `POST /playlists` · `POST /playlist/<id>/{toggle,delete,refresh}` · `POST /track/<pid>/<tid>/retry` · `POST /track/<pid>/<tid>/retag` · `POST /settings/test-ntfy`

**Two constraints any new UI code must respect:**
1. **Every POST needs `csrf_token()` in its form** — the `before_request` guard rejects tokenless requests with 400.
2. **No inline `<script>`** — the CSP is `script-src 'self'`. The theme toggle and the log tail live in `static/theme.js` and `static/logs.js` for exactly this reason. Inline handlers fail silently in the browser, not in tests.

### Security boundary

**The UI has no authentication.** Compose binds it to loopback; exposing it to a
LAN means putting a reverse proxy with auth in front. Given that, the browser is
the confused deputy, so:

- **Every mutating route requires a session-backed CSRF token.** Loopback
  binding does not mitigate CSRF — the browser is inside the trust boundary. A
  token is used rather than an Origin/Referer check so reverse-proxy
  deployments keep working. Templates render it via `csrf_token()`.
- The Flask secret key comes from `UI_SECRET_KEY`, else a generated key
  persisted in the `settings` table under `_ui_secret_key` (not in
  `SETTINGS_SPEC`, so it never renders on the settings page). It must be stable
  across restarts or CSRF tokens in open pages break.
- Session cookie is HttpOnly + SameSite=Lax; set `UI_COOKIE_SECURE=1` when a
  proxy terminates TLS (forcing it would break plain-http loopback).
- CSP plus `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`.
- **`/logs` exposes the daemon's log tail with no authentication**, like every
  other route. Keep credentials out of log messages — that page is the easiest
  way for them to leak if the UI is ever exposed beyond loopback.
- **`.dockerignore` is load-bearing.** The Dockerfile does `COPY . /app/`;
  without it, a local `docker compose up --build` bakes `.env`, the production
  DB, the music library and slskd's keys into image layers. CI asserts none of
  those paths exist in the built image.

Verified-and-mitigated (do not "fix" again without reading the reasoning):
`sanitize_table_name` collapses every non-word character to `_`, so no quote,
semicolon or comment delimiter can reach a table-name f-string — the dynamic
SQL is **not** injectable. Jinja autoescaping neutralises Spotify-supplied
metadata in templates. Peer-supplied filenames never reach a local path
unnormalised.

### State machine per track

`downloaded`, `attempts`, `suspended_until`, `path`, `last_upgrade_check`.

1. New Spotify track → row inserted with `downloaded=0`.
2. `startup_check` (one-shot) matches against files already on disk.
3. `get_pending_tracks` returns rows where `downloaded=0` AND suspension elapsed.
4. On failure, `update_download_status(success=False)` increments `attempts`; at `MAX_ATTEMPTS_BEFORE_SUSPEND` (2) the row is suspended for 2 days.
5. On verified success, `clear_tried_entries` deletes that track's rows from
   `pl_<…>_tried`, so a filename rejected once isn't blacklisted forever.

Files enter via the shared `/downloads` volume, then move to `/playlists/<table>/` after tagging.

### Environment knobs

Deploy-time / infra (env only):
- `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, `SLSKD_API_KEY`, `NTFY_URL`, `NTFY_TOPIC`
- `SLSKD_HOST_URL`, `SLSKD_URL_BASE`, `SLSKD_DOWNLOADS_DIR`, `SLSKD_PLAYLISTS_DIR`
- `CYCLE_INTERVAL_SECONDS` (300), `CYCLE_ERROR_BACKOFF_SECONDS` (60)
- `HEARTBEAT_FILE`, `HEARTBEAT_STALE_SECONDS`
- `LOGLEVEL`, `LIB_LOGLEVEL`, `TIMEZONE`
- `PUID`/`PGID` (compose user override; the image itself is `USER 1000:1000`)
- `UI_ENABLED` (1), `UI_BIND_ADDR` (0.0.0.0), `UI_PORT` (8000), `UI_SECRET_KEY`, `UI_COOKIE_SECURE` (0)
- `SPOTIFY_PLAYLIST_URLS` — read **only on first boot** to seed the DB

UI-editable (env is a default; DB override wins). All are validated on save —
see `SETTINGS_SPEC` and `validate_setting` in `spotify_slsk/webui/__init__.py`:
- `MIN_MATCH_SCORE` (0.62), `SLSKD_PREFERRED_FORMATS`, `SLSKD_MIN_PEER_UPLOAD_SPEED` (0),
  `SLSKD_MIN_EFFECTIVE_MP3_KBPS` (280), `SLSKD_EARLY_STOP_RESPONSES` (20),
  `SLSKD_WAIT_TIMEOUT` (60), `UPGRADE_CHECK_INTERVAL_HOURS` (168)

`SLSKD_MAX_RETRIES` was removed: it was refreshed every cycle and exposed in the
UI, but nothing ever read it, and neither did the `max_attempts` parameter
threaded through the download call chain. The real per-track cap is
`db.MAX_ATTEMPTS_BEFORE_SUSPEND`.

### Gotchas

- **Table names come from Spotify playlist titles.** `sanitize_table_name` is lossy-but-stable; changing its output orphans existing rows. **Colliding sanitized names share a table** (`"Chill"` and `"chill"` both → `pl_chill`), which also means deleting one playlist drops the other's rows. Known, unfixed — a fix needs a migration.
- **Matchers are not unified by design.** rapidfuzz in `soulseek_api.py` (search results) vs difflib in `app.py` (local files / DB) serve different phases. `app.py` additionally has two matchers of its own: `score_track_match` (threshold-based, gates a *destructive* accept/delete) and `_looks_like_match` (plausibility, non-destructive reconcile). The differing risk profiles are why they haven't been merged.
- **`startup_check` passes `destructive=False`** so a bad match never deletes an existing library file — regular downloads *do* delete on mismatch. There are tests pinning both halves; keep them.
- **Validated settings matter**: `_reload_settings` silently keeps the previous value when a stored value won't parse, so an unvalidated save would report success for a setting that never applies.
- **`slskd_api==0.1.5`** is pinned. Transfer-state substrings (`"completed, succeeded"`, `"failed"`) are matched; bumping may break `wait_for_completion`.
- **SQLite WAL mode** means the DB file is always accompanied by `-wal` and `-shm` files. Backups must capture all three or run `PRAGMA wal_checkpoint(TRUNCATE)` first.
- **Tracks removed from a Spotify playlist upstream are kept locally** — the library is append-only. `tests/test_pipeline.py` pins this so a change is deliberate.
- **Schema version is 4.** Upgrading from an older DB is automatic and tested (`TestSchemaMigrations`): the ALTER TABLE guards in `_ensure_playlists_meta_table` handle v2 DBs, `create_table` adds `last_upgrade_check` to older per-playlist tables, `CREATE TABLE IF NOT EXISTS` handles settings, and v4 added `cycle_history` (one row per completed cycle, capped to ~200 rows by `db.record_cycle`). The `tried_files` column on per-playlist tables is dead — nothing reads it; it is retained only so existing DBs need no migration.
