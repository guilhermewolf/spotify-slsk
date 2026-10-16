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
make check                           # compile + lint + typecheck + test + audit
make check PYTHON=.venv/bin/python   # against a virtualenv
```

Python **3.12** (`requires-python` in `pyproject.toml`, the Docker base image
and CI all agree; the Makefile defaults to `python3.12`). Individual targets:
`make compile`, `make lint`, `make typecheck`, `make test`, `make audit`,
`make docker-build`.

- `make lint` — ruff with `E,F,W,I,B,UP` (config in `pyproject.toml`). Fix
  findings rather than adding ignores.
- `make typecheck` — mypy over `spotify_slsk/` (`ignore_missing_imports`,
  `check_untyped_defs`). Every public function is annotated; keep it that way.
- `make test` — 638 tests. Fast (~5s); no network, no external services. The
  real-FLAC decode tests need the `flac` CLI (CI apt-installs it) and skip with
  a reason without it.
- `make audit` — `pip-audit` against the pinned `requirements.txt`. Advisories
  fail CI, so dependency CVEs surface on a PR rather than in production.
- CI also builds the image and asserts it runs as uid 1000 and that no
  `.env` / `data/` / `playlists/` / `slsk_app/` path leaked into it.

`pyproject.toml` holds project metadata and tool config; `requirements.txt` /
`requirements-dev.txt` remain the **pinned lock** used by the Dockerfile,
`make install` and `pip-audit`. Bump pins there.

`test.py` at the repo root is a gitignored scratchpad.

## Test isolation (important)

`tests/conftest.py` makes the suite **incapable of reaching production**, and
this is deliberate — do not weaken it:

- Environment values are **overridden, not defaulted**. The test modules'
  `os.environ.setdefault(...)` calls are no-ops when the developer's shell
  already exports real values; before conftest existed, running `pytest` with a
  sourced `.env` pointed the suite at the real slskd and at the real music
  library — the directory `importer.process._reject_and_log` will `os.remove` from.
- `SLSKD_DOWNLOADS_DIR`, `SLSKD_PLAYLISTS_DIR`, `HEARTBEAT_FILE` and the
  spotipy cache all point into a per-session temp tree.
- Constructing a real `slskd_api.SlskdClient` raises, as does any real outbound
  HTTP request. Tests that need those boundaries mock them explicitly
  (usually `slskd.client.get_client`).
- Monkeypatch **where a name is looked up**, not where it is defined. Modules
  in `slskd/` reach the client through the module (`slskd_client.get_client()`,
  `slskd_client.interruptible_sleep()`), so one patch of `spotify_slsk.slskd.client`
  covers every caller; names imported with `from x import y` (e.g.
  `importer.process.tag_audio_file`) must be patched on the importing module.
- `tests/test_isolation.py` asserts all of the above, so weakening conftest
  fails the suite rather than silently re-pointing it at production.

Use the `sandbox_dirs` fixture for anything touching the filesystem.

## Layout

```
spotify_slsk/
  __main__.py          entrypoint: python -m spotify_slsk -> daemon.main
  daemon.py            main(), startup order, cycle loop, heartbeat, signals, wake
  config.py            every tunable: env defaults, reload(conn), SETTINGS_SPEC
  notify.py            ntfy
  models.py            Track + shared type aliases (Candidate, IsBlocked)
  runtime_state.py     in-process observability + manual-grab queue
  log_config.py utils.py
  db/                  SQLite: connection (+migrations), playlists, tracks,
                       settings, cycles, blocklist, history; re-exported by db/__init__
  spotify/             client.py (client + name lookup), sync.py (fetch/diff/insert)
  slskd/               models.py (typed JSON), client.py, search.py, transfers.py
  matching/            normalize.py (shared), candidates.py (search phase),
                       tracks.py (local phase)
  importer/            tags.py (read/write tags + ISRC, audio length), process.py (verify,
                       tag, move, reject/delete/blocklist), audio_check.py (fake lossless)
  library/             acquire.py (download + upgrade a track), reconcile.py (startup_check),
                       health.py (library health scan + quarantine)
  webui/               app factory + blueprints (templates/, static/)
tests/                 pytest suite (conftest.py enforces isolation)
  fixtures/slskd/      slskd payloads in the real wire shape
```

Dependency direction: `daemon` → `library`/`importer`/`spotify`/`slskd` →
`matching` → `config`/`db`/`models`. Nothing imports `daemon` or `webui`;
`webui` reaches the daemon only through `runtime_state` and the
`wake_callback`. Imports between modules are relative (`from ..db import ...`).
`from spotify_slsk import db` keeps working: `db/__init__.py` re-exports every
public name. The daemon resolves `./data/playlist_tracks.db`
relative to the working directory — run it from the repo root, which is what
the container does (`WORKDIR /app`).

## Architecture

Single long-running process (`spotify_slsk/daemon.py::main`) with two threads:

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
   for two days — turning a brief outage into a self-inflicted backlog. This
   includes a missing `SLSKD_API_KEY` (slskd-api raises `ValueError` building
   the client): the client is built inside the retry loop.
7. One-shot on-disk reconciliation (`startup_check`) — matches existing files to DB rows so they aren't re-downloaded.
8. Enter the cycle loop: `_touch_heartbeat()` → `config.reload(conn)` → iterate `list_playlists(only_enabled=True)`. On any unhandled exception, log and back off `CYCLE_ERROR_BACKOFF_SECONDS`. Between cycles, `_wake_event.wait(CYCLE_INTERVAL_SECONDS)` — interruptible by the UI's `wake_now()`.
9. On shutdown, close the SQLite connection in a `finally`.

Docker `HEALTHCHECK` compares `HEARTBEAT_FILE` mtime against `HEARTBEAT_STALE_SECONDS` (default 900s). The heartbeat is touched **per playlist and per track**, not once per cycle: one track can occupy minutes (60s search + up to 300s transfer + 60s post-processing per candidate), so a coarser touch made a busy daemon indistinguishable from a wedged one.

### Module responsibilities

- **`daemon.py`** — `main()` and the startup order above; `process_playlist` (sync → pending downloads → throttled upgrade pass), `_process_manual_grabs`, `_run_startup_reconciliation`, `block_until_slskd_healthy`, the `_shutdown`/`_wake_event` Events, `wake_now()` and the heartbeat. The only module allowed to drive downloads and imports.
- **`config.py`** — single source of truth for tunables. Deploy-time values are read from env once; UI-editable ones are module attributes rebound by `reload(conn)` at each cycle start, so **read them as `config.NAME` at call time**, never `from .config import NAME`. Also holds `SETTINGS_SPEC` + `validate_setting` (used by the settings page) and `normalize_ext_list`.
- **`spotify/`** — `client.setup_spotify_client()` (returns `None` without credentials; token cache at `SPOTIPY_CACHE_PATH`), `client.fetch_playlist_name` (web UI, retries transport errors); `sync.fetch_and_compare_tracks` (snapshot short-circuit, diff, insert, duration backfill), `sync.migrate_env_playlists` (first boot).
- **`slskd/models.py`** — tolerant typed views of slskd JSON: `SearchResponse`/`PeerFile` (camelCase in; `ext` from the filename because `extension` is often `""`; `lockedFiles` kept separate and never offered), `Search`, `Transfer` with `outcome` (`TransferOutcome` or `None` while pending) and `peer_fault`. All slskd JSON is read through these. `remote_basename` lives here.
- **`slskd/client.py`** — `get_client()` (lazy, shared by the daemon thread), `new_client()` (for other threads), shutdown wiring (`set_shutdown_event`, `interruptible_sleep`, `shutting_down`), `wait_for_slskd_healthy`.
- **`slskd/search.py`** — query waterfall (multi-query + CJK passthrough), `_run_one_search` (server-side `responseLimit` early stop; the search is **always deleted in `finally`**), `search_and_download` (falls through on no *usable* candidates; records per-query search history), `download_and_verify`, and `interactive_search` (read-only, collect-everything, own client).
- **`slskd/transfers.py`** — `grab_candidate`, the single enqueue/wait/verify step shared by auto and manual grabs (tried + history on any failure; blocklist only on definitive peer faults; a raised API error records nothing permanent), `wait_for_completion`, completion grace, `last_download`.
- **`matching/`** — see "Matchers" under Gotchas. `normalize.py` (folding, artist splitting, version keywords/tiers — shared), `candidates.py` (`evaluate_candidates`: blocklist + format allowlist + fixed 320 floor for MP3s with a known bitrate + effective-bitrate floor + duration vs Spotify + the strict name rules in `filename_match`; returns every file with `accepted` and a human `reason`; `sort_candidates`: version tier > format > bitrate > peer upload speed), `tracks.py` (`score_track_match`, `_looks_like_match`, difflib helpers).
- **`importer/`** — `tags.py`: `write_tags` (raises; used by the UI re-tag), `tag_audio_file` (never raises; daemon), `extract_metadata_from_file`, `audio_length_seconds`, `read_isrc`/`normalize_isrc`. `process.py`: `process_downloaded_file` (verify against the requested track, ISRC check, untagged filename+duration fallback, lossless check, tag, move, record verdict) and `_reject_and_log` (the only place a downloaded file is deleted). `audio_check.py`: the fake-lossless detector (see Validation below).
- **`library/`** — `acquire.py`: `handle_track_download`, `try_upgrade_track` (also replaces a *suspect* lossless file with a lossless copy). `reconcile.py`: `startup_check` and its local file index (non-destructive). `health.py`: `verify_file`/`scan_step` (the report-only library health scan) and `quarantine_track` (daemon only).
- **`notify.py`** — ntfy; silent when unconfigured.
- **`db/`** — SQLite persistence, split by table family and re-exported from `db/__init__.py`. WAL mode + `busy_timeout=5000` + `PRAGMA user_version` migrations (`db/connection.py`). Per-playlist dynamic tables (`pl_<sanitized_name>` + `pl_<…>_tried`) plus a global `playlists_meta` catalogue keyed by Spotify id, a global `settings` key-value bag, a global `blocklist` (peer files, or whole peers when `filename` is NULL), a global `history` event log and a global `file_checks` table (validation verdicts, `db/checks.py`). `get_setting(conn, key, default)` prefers DB, falls back to env, then default — so any env var becomes a mutable setting for free.
- **`spotify_slsk/webui/`** — Flask dashboard (see next section).
- **`spotify_slsk/runtime_state.py`** — in-process observability: thread-safe current-activity dict, a bounded log ring buffer (fed by a handler installed in `log_config`), per-track slskd search history (with accepted/rejected counts and reasons), interactive-search results keyed by (playlist_id, track_id) with a 10-minute TTL, the manual-grab and quarantine queues the UI fills and the daemon drains, and `touch_heartbeat()` (shared by the daemon and the importer). All of it is per-process and resets on restart — deliberately not persisted.
- **`spotify_slsk/utils.py`** — `sanitize_table_name`, `get_playlist_id`, `fmt_duration`.
- **`spotify_slsk/log_config.py`** — timezone-aware logging; `LIB_LOGLEVEL` (default WARNING) silences noisy third-party loggers independently of app `LOGLEVEL`.

### Web UI

Flask + Jinja2 + Pico.css + htmx. **Assets are self-hosted** in `spotify_slsk/webui/static/`
(Pico 2.0.6, htmx 1.9.12) — not on a CDN. A compromised CDN could drive the
unauthenticated mutating routes, and self-hosting is what allows the CSP to stay
at `script-src 'self'`. `spotify_slsk/webui/static/app.css` is the small design layer on top
of Pico. Note the classless Pico build ships **no** `.secondary`/`.outline`
classes; `app.css` defines them.

`webui/__init__.py::create_app` owns everything cross-cutting (secret key,
cookie flags, CSRF guard, security headers/CSP, per-request DB connection, the
header health badge) and registers one blueprint per area: `playlists`
(dashboard, playlist page, playlist mutations), `tracks` (track page,
retry/re-tag, interactive search/grab), `wanted` (wanted, history, blocklist),
`health` (library health report, scan/quarantine/ignore),
`settings` (settings, ntfy probe), `system` (file, logs, activity, healthz).
Endpoints are blueprint-qualified — templates use `url_for('playlists.dashboard')`,
not `url_for('dashboard')`. URLs are pinned by
`test_url_map_is_unchanged_by_the_blueprint_split`.

Routes:
- `GET /` — dashboard: library totals + playlist cards (htmx-refreshed every 15s as one swap)
- `GET /playlist/<id>` — track list with status filter + `?q=` search
- `GET /track/<pid>/<tid>` — per-track state, search history with rejection reasons, persistent history, rejected filenames, interactive search
- `POST /track/<pid>/<tid>/search` · `GET /track/<pid>/<tid>/search` (htmx partial, polls while running) · `POST /track/<pid>/<tid>/grab` — interactive search and manual grab. A grab must name a result of that track's own search; grabbing a rejected one renders `grab_confirm.html` and needs `force=1`
- `GET /wanted` · `POST /wanted/search` — missing tracks across enabled playlists; bulk re-queue (selected, or `all=1` behind a `<details>` confirm)
- `GET /history?event=&playlist=&before_id=` — persistent events, 200 per page
- `GET /blocklist` · `POST /blocklist/<id>/delete` · `POST /blocklist/ban-user`
- `GET /library-health?problem=&playlist=&ignored=` · `POST /library-health/scan` · `POST /library-health/quarantine` (`only=pid:tid` or checked `tracks`) · `POST /library-health/ignore[?undo=1]` — scan findings with evidence; scan and quarantine are queued for the daemon
- `GET /settings` — grouped, validated tunables
- `GET /logs` · `GET /logs.json?since=&limit=` · `GET /activity.json` — live log tail and current daemon phase
- `GET /file?rel=` — serves a downloaded file for the in-browser audio preview; `rel` is resolved against the playlists root and re-checked with `commonpath` after symlink normalisation
- `GET /healthz` — JSON health (schema version, heartbeat age, reason)
- `POST /playlists` · `POST /playlist/<id>/{toggle,delete,refresh}` (delete requires `confirm=1`, sent by the dashboard's `<details>` confirm) · `POST /track/<pid>/<tid>/retry` · `POST /track/<pid>/<tid>/retag` · `POST /settings/test-ntfy`

**Only the daemon thread downloads, moves files or imports.** The interactive
search runs on a UI background thread with its own slskd client (not the
daemon's shared session) and a blocklist snapshot, and never writes. A grab is
queued in `runtime_state`; the daemon drains the queue at the top of each cycle
and after each playlist (`daemon._process_manual_grabs`). Quarantine requests
work the same way (`daemon._process_quarantines`, top of each cycle), and
"Scan library" only sets the `_library_scan_pending` settings flag that
`daemon._run_library_scan` reads after the playlists.

**Two constraints any new UI code must respect:**
1. **Every POST needs `csrf_token()` in its form** — the app-level `before_request` guard (it covers every blueprint) rejects tokenless requests with 400.
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

`downloaded`, `attempts`, `suspended_until`, `path`, `last_upgrade_check`, `duration_ms` (Spotify, v5), `isrc` (Spotify, v6).

1. New Spotify track → row inserted with `downloaded=0`.
2. `startup_check` (one-shot) matches against files already on disk.
3. `get_pending_tracks` returns rows where `downloaded=0` AND suspension elapsed.
4. On failure, `update_download_status(success=False)` increments `attempts`; at `MAX_ATTEMPTS_BEFORE_SUSPEND` (2) the row is suspended for 2 days.
5. On verified success, `clear_tried_entries` deletes that track's rows from
   `pl_<…>_tried`, so a filename rejected once isn't blacklisted forever.
6. **Blocklist policy.** Every failure goes to the track's `_tried` list and
   history. Only a *definitive peer-file fault* also adds the peer file to the
   global `blocklist` (which survives retries and success): slskd reporting the
   transfer failed/errored/rejected/aborted (`candidate["peer_fault"]`), or a
   post-download rejection of the file itself — no usable tags, unreadable
   audio, a real length far from what the peer advertised, or a 24-bit file
   that is padded 16-bit audio (`padded_16`, an exact test). A `fake` or
   `upsampled` spectrum verdict deletes the file and goes to `_tried` + history
   only: those rest on ~3-5 dB margins, and a false positive must cost one track
   a re-search, not ban a good file for every track. Our wait timeout,
   "Queued, Remotely" stalls, slskd not listing the transfer yet, a file not
   found afterwards, a wrong-track mismatch ("no match"/"low score"/filename or
   Spotify-duration miss), shutdown and reconcile never blocklist: the same file
   can be right for another track, and a global ban would hide it there.
7. **Completion grace.** slskd can report "Completed, Succeeded" while the file
   is still under `/downloads/incomplete/`. `find_file_in_downloads` never
   searches `incomplete`, and `wait_for_completion` polls for the finished file
   for up to `COMPLETION_GRACE_SECONDS` (30 s, 1 s steps, shutdown-interruptible)
   before calling it "file not found after download".
8. A download with no title/artist tags is accepted only when the daemon knows
   the expected track AND the file's name passes the strict candidate rules
   (`matching.candidates.filename_match`, peer path preferred so its folder can supply
   the artist) AND the real (mutagen) duration is within
   `SLSKD_MAX_DURATION_DIFF_SECONDS` of Spotify's. Unknown durations, or the
   setting at 0, mean reject.
9. A manual grab that fails does not increment `attempts`.
10. **Quarantine** (`library.health.quarantine_track`, daemon only, requested
    from /library-health): the file moves to
    `<playlists_root>/.quarantine/<table>/<basename>` (" (2)" etc. on a
    collision; never deleted), the row goes back to `downloaded=0` with no path,
    attempts or pause, its basename joins `_tried`, its `file_checks` row is
    dropped and history records `quarantined`. The quarantine dir is outside
    every `<root>/<table>` that `startup_check` indexes and the scan walks DB
    rows, so nothing re-imports it. Purging is manual (README). The request
    carries the path the operator saw flagged: if the row points at another
    file by the time the daemon runs, or the file resolves (realpath +
    commonpath) outside the playlists root, nothing moves and history records
    `quarantine_skipped` with the reason.

Files enter via the shared `/downloads` volume, then move to `/playlists/<table>/` after tagging.

### Environment knobs

Deploy-time / infra (env only):
- `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, `SLSKD_API_KEY`, `NTFY_URL`, `NTFY_TOPIC`
- `SLSKD_HOST_URL`, `SLSKD_URL_BASE`, `SLSKD_DOWNLOADS_DIR`, `SLSKD_PLAYLISTS_DIR`
- `CYCLE_INTERVAL_SECONDS` (300), `CYCLE_ERROR_BACKOFF_SECONDS` (60), `SLSKD_HTTP_TIMEOUT` (30)
- `HEARTBEAT_FILE`, `HEARTBEAT_STALE_SECONDS`
- `LOGLEVEL`, `LIB_LOGLEVEL`, `TIMEZONE`
- `PUID`/`PGID` (compose user override; the image itself is `USER 1000:1000`)
- `UI_ENABLED` (1), `UI_BIND_ADDR` (0.0.0.0), `UI_PORT` (8000), `UI_SECRET_KEY`, `UI_COOKIE_SECURE` (0)
- `SPOTIFY_PLAYLIST_URLS` — read **only on first boot** to seed the DB

UI-editable (env is a default; DB override wins). All are validated on save —
see `SETTINGS_SPEC` and `validate_setting` in `spotify_slsk/config.py`:
- `MIN_MATCH_SCORE` (0.62), `SLSKD_PREFERRED_FORMATS`, `SLSKD_MIN_PEER_UPLOAD_SPEED` (0),
  `SLSKD_MIN_EFFECTIVE_MP3_KBPS` (280), `SLSKD_EARLY_STOP_RESPONSES` (20),
  `SLSKD_MAX_DURATION_DIFF_SECONDS` (15; 0 disables the duration filter and the
  untagged fallback), `SLSKD_WAIT_TIMEOUT` (60), `HISTORY_RETENTION_DAYS` (90,
  pruned once per cycle), `UPGRADE_CHECK_INTERVAL_HOURS` (168),
  `LOSSLESS_CHECK_ENABLED` (1, kind `bool`), `LOSSLESS_FAKE_CUTOFF_HZ` (19500),
  `LOSSLESS_MIN_DROP_DB` (15), `LIBRARY_SCAN_INTERVAL_HOURS` (0 = manual only)

Every `SETTINGS_SPEC` key must be placed in a group in `settings.html`: the
form clears every key it doesn't send (`test_every_setting_is_rendered`).

The MP3 floors are two rules: an MP3 whose reported (or filename-inferred)
bitrate is known and below 320 is **always** rejected (hardcoded, by design);
`SLSKD_MIN_EFFECTIVE_MP3_KBPS` only bounds the size/duration-derived bitrate.

`SLSKD_MAX_RETRIES` was removed: it was refreshed every cycle and exposed in the
UI, but nothing ever read it, and neither did the `max_attempts` parameter
threaded through the download call chain. The real per-track cap is
`db.MAX_ATTEMPTS_BEFORE_SUSPEND`.

### Gotchas

- **Table names come from Spotify playlist titles.** `sanitize_table_name` is lossy-but-stable; changing its output orphans existing rows. **Colliding sanitized names share a table** (`"Chill"` and `"chill"` both → `pl_chill`), which also means deleting one playlist drops the other's rows. Known, unfixed — a fix needs a migration.
- **Matchers are not unified by design.** Token rules in `matching/candidates.py` (search results, judged by file *name*) vs difflib in `matching/tracks.py` (local files / DB, judged by *tags*) serve different phases. `tracks.py` has two matchers of its own: `score_track_match` (threshold-based, gates a *destructive* accept/delete) and `_looks_like_match` (plausibility, non-destructive reconcile). The differing risk profiles are why they haven't been merged. They *do* share the normalisation helpers in `matching/normalize.py` (`fold`, `ARTIST_SPLIT_RE`, `credited_names`, `version_keywords`). There is no rapidfuzz any more.
- **Wrong-song guards (do not loosen without a regression case).** ~15% of downloads once pointed at the wrong song; `tests/test_wrong_song_regressions.py` holds the real cases. The rules:
  - slskd peer paths use **backslashes** — always `remote_basename` (or `PeerFile.basename`), never `os.path.basename`. Candidates keep the full peer path in `filename` (slskd needs it to enqueue) and the real basename in `basename` (what `_tried` stores).
  - Candidate title evidence comes from the **basename only**: every expected title word (minus feat. credits, neutral words, years) must be present and make up ≥ `MIN_TITLE_COVERAGE` of the basename's title-ish words. Artist evidence must be a **whole name** (`artist_in`), and may come from the peer's folders (shares are `Artist\Album\NN - Title`).
  - Versions are compared **symmetrically** (`version_keywords`: remix, dub, vip, club mix, live, acoustic, instrumental, karaoke, cover, sped up, slowed, nightcore, 8d, bootleg, rework, flip, named "X Mix"/"X Edit"...). Original/Extended Mix, Radio Edit, Album Version and Remastered are neutral so `version_tier` can still prefer the Extended Mix.
  - `score_track_match` requires an **artist overlap** and the same version (remixer included); there is no title-only accept.
  - A download is verified against the **requested track only** (`process_downloaded_file(..., track_id=)`); it is never re-homed onto another row. Only track-less calls (reconcile) use the playlist-wide `find_closest_match`.
- **`startup_check` passes `destructive=False`** so a bad match never deletes an existing library file — regular downloads *do* delete on mismatch. There are tests pinning both halves; keep them.
- **Validated settings matter**: `config.reload` silently keeps the previous value when a stored value won't parse, so an unvalidated save would report success for a setting that never applies.
- **slskd versions.** Compose pins the slskd server at **0.26.1** (0.26 moved `global`/`groups` under `transfers` and renamed `integration` → `integrations`; README has the migration). `slskd-api==0.2.4` is a thin HTTP wrapper; the transfer-state strings come from the **server**. `Transfer.outcome` treats any `"Completed, X"` other than `"Completed, Succeeded"` as a failure (Rejected, TimedOut, Errored, Aborted, Cancelled); only Errored/Rejected/Aborted (and "failed") are a peer fault. `tests/test_slskd_contract.py` pins every production state plus 401/404/500/connection/timeout handling at every call site — run it when bumping either.
- **slskd fields are camelCase** (`bitRate`, `bitDepth`, `sampleRate`, `isVariableBitRate`, `uploadSpeed`). A lowercase `bitrate` read silently returned None for years. Read slskd JSON only through `slskd/models.py`; `tests/fixtures/slskd/` holds payloads in the real shape.
- **slskd HTTP timeout.** `new_client` passes `timeout=SLSKD_HTTP_TIMEOUT` (default 30s) to `SlskdClient`; slskd-api's default is none, and a slskd that accepts but never answers would otherwise block the daemon thread until the healthcheck restarts the container. Every slskd call is a short request, so 30s is generous.
- **SQLite WAL mode** means the DB file is always accompanied by `-wal` and `-shm` files. Backups must capture all three or run `PRAGMA wal_checkpoint(TRUNCATE)` first.
- **Tracks removed from a Spotify playlist upstream are kept locally** — the library is append-only. `tests/test_pipeline.py` pins this so a change is deliberate.
- **Validation (Tier 3).** `importer/audio_check.py` decodes 3 x 10 s windows
  (middle 80%) with the `flac` CLI (WAV via stdlib), Welch-averages Hann frames
  (4096, silent frames dropped), smooths to ~170 Hz and finds the steepest drop
  above 11 kHz (mean 0.2-1 kHz below f minus 0.2-1 kHz above). `fake` also
  needs consistency: each third of the sampled audio must classify fake on its
  own with cutoffs within 1 kHz, else it is downgraded to suspect. fake /
  upsampled / padded_16 reject (only padded_16 blocklists, see Blocklist policy); suspect (19.5-20.5 kHz, MP3-320 vs brickwalled masters)
  and unknown never do. Margins are ~3-5 dB, so thresholds are settings; AAC-256
  transcodes are undetectable. Before download, `evaluate_candidates` rejects a
  16-bit FLAC under 400 kbps and demotes (`note`, sorted last within its format)
  a 24-bit >= 88.2 kHz FLAC under 1200 kbps. ISRC: equal -> overrides a low
  tag score, never a version mismatch; different -> **never rejects on its
  own** (singles/albums/remasters/compilations differ), only logged + recorded
  (`isrc_mismatch` on the `imported` event), and appended to the reason when
  another failure rejects (low score, version mismatch, real duration off via
  `_duration_off`). In the scan it is `evidence["isrc_note"]`: info only, not a
  problem, never in a bulk quarantine.
  Verdicts live in `file_checks` (one global table keyed by
  `(table_name, track_id)`, not columns on every pl_ table: one query for the
  health page, scan bookkeeping out of the track rows). Imports store theirs with
  `rules_version` 0; the scan (`health.RULES_VERSION`, bump it when the identity
  rules change) re-verifies identity and reuses an unchanged file's spectrum
  verdict. The scan is resumable without a cursor: a check matching path, size,
  mtime, RULES_VERSION and `audio_check.rules_key()` (the LOSSLESS_* thresholds +
  a logic version) is skipped, so changing a threshold and pressing Scan
  re-decodes and re-classifies. An `ignored` mark survives only while the
  file is the same. AcoustID fingerprinting is not implemented (follow-up).
- **Image.** The Dockerfile `apk add`s `flac` and strips numpy's bundled test
  suite; numpy (+ its OpenBLAS) is most of the ~76 MB the image grew in Tier 3.
- **Schema version is 6.** v6 adds `isrc` to every catalogued per-playlist
  table at connect time and clears snapshots so the next sync backfills it
  (same mechanism as v5, `_migrate_track_columns`; `backfill_durations` fills
  both with COALESCE), plus `file_checks` (`CREATE TABLE IF NOT EXISTS`).
  `TestSchemaMigrations.test_v5_db_upgrades_to_v6` builds a real v5 DB. v5 (`db/connection.py::_migrate_track_columns`, run from `_ensure_schema_version`) adds `duration_ms` to every catalogued per-playlist table at connect time — not lazily in `create_table`, because the UI reads tables of playlists the daemon hasn't synced since the upgrade — and clears every stored `snapshot_id` so the next sync re-reads Spotify and backfills durations (`db.backfill_durations`). `blocklist` and `history` are `CREATE TABLE IF NOT EXISTS`. `TestSchemaMigrations.test_v4_db_upgrades_to_v5` builds a real v4 DB. Upgrading from an older DB is automatic and tested (`TestSchemaMigrations`): the ALTER TABLE guards in `_ensure_playlists_meta_table` handle v2 DBs, `create_table` adds `last_upgrade_check` to older per-playlist tables, `CREATE TABLE IF NOT EXISTS` handles settings, and v4 added `cycle_history` (one row per completed cycle, capped to ~200 rows by `db.record_cycle`). The `tried_files` column on per-playlist tables is dead — nothing reads it; it is retained only so existing DBs need no migration.
