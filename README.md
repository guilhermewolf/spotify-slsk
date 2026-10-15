# spotify-slsk

Keeps a local music library in sync with your Spotify playlists by finding the
tracks on Soulseek via [`slskd`](https://github.com/slskd/slskd), tagging them,
and filing them into per-playlist folders. Runs unattended, with a small web
dashboard for adding playlists, watching progress and retrying what failed.

## Features

- **Spotify sync** — polls your playlists and picks up new tracks. Uses Spotify's
  `snapshot_id` to skip the expensive track fetch when a playlist hasn't changed.
- **Quality-aware search** — format allowlist, a fixed 320 kbps floor for MP3s
  whose bitrate is known, an effective-bitrate check from size and duration
  (`SLSKD_MIN_EFFECTIVE_MP3_KBPS`, catches 128 kbps files retagged as 320), and a
  version gate so a Live or Remix version isn't accepted for a studio track.
- **Version preference** — prefers Extended Mix over Original Mix over
  everything else, and can periodically re-search downloaded tracks for a
  better version.
- **Metadata tagging** — writes title/artist/album with `mutagen`, then moves
  the file into `/playlists/<playlist>/`.
- **Duration-aware matching** — candidates whose length differs from Spotify's
  by more than `SLSKD_MAX_DURATION_DIFF_SECONDS` are skipped, and an untagged
  download is accepted only if its filename and real duration both match.
- **Retry and back-off** — a track that fails twice is suspended for two days
  rather than retried forever; rejected filenames are remembered so the same bad
  file isn't downloaded again.
- **Blocklist** — peer files that slskd reports as failed/rejected, or that
  turn out broken after download (no tags, unreadable, truncated), are skipped
  for every track; whole users can be banned from the UI. A file that is merely
  the wrong song for one track is only skipped for that track.
- **History** — every grab, import, failure, upgrade and retry is kept for
  `HISTORY_RETENTION_DAYS`, with a filterable history page.
- **Web dashboard** — library totals, per-playlist progress, per-track status,
  why each search result was rejected, interactive search with manual grab, a
  Wanted page with bulk re-search, manual retry, and editable settings that
  apply without a restart.
- **Health** — `/healthz` plus a Docker `HEALTHCHECK` fed by a heartbeat file.
- **ntfy notifications** — optional; silently skipped when unconfigured.

## Requirements

- Docker and Docker Compose (recommended), or Python 3.12+
- A running [`slskd`](https://github.com/slskd/slskd) instance and a Soulseek account
- Spotify API credentials (client ID/secret) — playlists must be public

## Quick start

```bash
git clone https://github.com/guilhermewolf/spotify-slsk.git
cd spotify-slsk
cp .env-example .env      # then fill it in
docker compose up --build
```

The dashboard is then on <http://127.0.0.1:8000>. Add playlists there — the
`SPOTIFY_PLAYLIST_URLS` variable is only read on the very first boot to seed an
empty database.

## Configuration

Set these in `.env`. Everything has a working default except the credentials.

| Variable | Description |
|---|---|
| `SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET` | Spotify API credentials (required) |
| `SLSKD_HOST_URL` | slskd base URL (default `http://slskd:5030`) |
| `SLSKD_API_KEY` | API key configured in `slskd.yml` (required) |
| `SLSKD_DOWNLOADS_DIR` | Where slskd writes downloads (default `/downloads`) |
| `SLSKD_PLAYLISTS_DIR` | Where tagged files are filed (default `/playlists`) |
| `SPOTIFY_PLAYLIST_URLS` | Comma-separated playlist URLs; **first boot only** |
| `NTFY_URL` / `NTFY_TOPIC` | Optional push notifications |
| `CYCLE_INTERVAL_SECONDS` | Seconds between sync cycles (default 300) |
| `UI_ENABLED` / `UI_BIND_ADDR` / `UI_PORT` | Dashboard toggle and bind (default `1`, `0.0.0.0`, `8000`) |
| `UI_SECRET_KEY` | Flask secret. Generated and stored in the DB if unset |
| `UI_COOKIE_SECURE` | Set `1` when a reverse proxy terminates TLS |
| `PUID` / `PGID` | UID/GID for the container (default `1000:1000`) |
| `LOGLEVEL` / `LIB_LOGLEVEL` / `TIMEZONE` | Logging verbosity and timestamps |

These are also editable from the **Settings** page, where the database value
overrides the environment variable and changes apply on the next cycle:
`MIN_MATCH_SCORE`, `SLSKD_PREFERRED_FORMATS`, `SLSKD_MIN_PEER_UPLOAD_SPEED`,
`SLSKD_MIN_EFFECTIVE_MP3_KBPS`, `SLSKD_EARLY_STOP_RESPONSES`,
`SLSKD_MAX_DURATION_DIFF_SECONDS`, `SLSKD_WAIT_TIMEOUT`,
`HISTORY_RETENTION_DAYS`, `UPGRADE_CHECK_INTERVAL_HOURS`.

## Upgrading slskd to 0.26

The bundled compose files now pin `slskd/slskd:0.26.1` (previously 0.23.1).
slskd 0.26 **refuses to start** with a 0.23-era `slskd.yml`, because config
keys moved ([slskd#1672](https://github.com/slskd/slskd/pull/1672)). Before
upgrading, edit `slsk_app/slskd.yml`:

| 0.23 key | 0.26 key |
|---|---|
| `global.upload` / `global.limits` / `global.download` | `transfers.upload` (with `limits` nested under it) / `transfers.download` |
| `groups` | `transfers.groups` |
| `integration` (webhooks, scripts, ...) | `integrations` |
| `permissions.file.mode` | `transfers.download.destination.permissions.mode` |

For example:

```yaml
# 0.23                         # 0.26
global:                        transfers:
  upload:                        upload:
    slots: 20                      slots: 20
  download:                      download:
    slots: 500                     slots: 500
groups: { ... }                  groups: { ... }
integration: { ... }           integrations: { ... }
```

Compare against slskd's
[example config for 0.26.1](https://github.com/slskd/slskd/blob/0.26.1/config/slskd.example.yml)
if slskd still logs a configuration error. Nothing changes on the
spotify-slsk side: the transfer states it reads (`Completed, Succeeded`,
`Queued, Remotely`, ...) are the same, and they are pinned by
`tests/test_slskd_contract.py`.

## Security

**The dashboard has no authentication.** Compose binds it to `127.0.0.1` for
that reason. If you expose it beyond loopback, put a reverse proxy with
authentication in front of it, and set `UI_COOKIE_SECURE=1` if that proxy
terminates TLS.

State-changing actions are CSRF-protected, static assets are self-hosted (no
CDN), and security headers including a CSP are sent on every response. The
container runs as a non-root user, and `.dockerignore` keeps your `.env`,
database and music library out of the image — do not remove it.

## Development

```bash
python3 -m venv .venv
make install PYTHON=.venv/bin/python
make check   PYTHON=.venv/bin/python

# run the daemon outside Docker (needs a .env and a reachable slskd)
.venv/bin/python -m spotify_slsk
```

The application lives in the `spotify_slsk/` package; `tests/` sits alongside
it. Run from the repo root — the SQLite path is resolved relative to the
working directory.

`make check` runs the same compile, lint, test and dependency-audit steps as
CI. The test suite is hermetic: `tests/conftest.py` forces fake credentials, an
unreachable slskd host and temporary download/playlist directories, and makes a
real slskd client or outbound HTTP request raise. It cannot touch your library
or your database.

See [CLAUDE.md](CLAUDE.md) for architecture, invariants and the reasoning behind
the less obvious design decisions.

## Backups

SQLite runs in WAL mode, so `data/playlist_tracks.db` is always accompanied by
`-wal` and `-shm` files. Copy all three, or run
`PRAGMA wal_checkpoint(TRUNCATE);` before backing up the single file.

## Releases

Pushes to `main` publish `:main`, `:sha-<short>` and `:latest`. Tagging
publishes the version tags without moving `:latest`, which is intentional so
deployments pinned to `:latest` follow `main`:

```bash
git tag v1.0.0 && git push origin v1.0.0
```
