# spotify-slsk

Keeps a local music library in sync with your Spotify playlists by finding the
tracks on Soulseek via [`slskd`](https://github.com/slskd/slskd), tagging them,
and filing them into per-playlist folders. Runs unattended, with a small web
dashboard for adding playlists, watching progress and retrying what failed.

## Features

- **Spotify sync** — polls your playlists and picks up new tracks. Uses Spotify's
  `snapshot_id` to skip the expensive track fetch when a playlist hasn't changed.
- **Quality-aware search** — format allowlist, bitrate floors (including an
  effective-bitrate check that catches 128 kbps files retagged as 320), and a
  version gate so a Live or Remix version isn't accepted for a studio track.
- **Version preference** — prefers Extended Mix over Original Mix over
  everything else, and can periodically re-search downloaded tracks for a
  better version.
- **Metadata tagging** — writes title/artist/album with `mutagen`, then moves
  the file into `/playlists/<playlist>/`.
- **Retry and back-off** — a track that fails twice is suspended for two days
  rather than retried forever; rejected filenames are remembered so the same bad
  file isn't downloaded again.
- **Web dashboard** — library totals, per-playlist progress, per-track status
  and history, manual retry, and editable settings that apply without a restart.
- **Health** — `/healthz` plus a Docker `HEALTHCHECK` fed by a heartbeat file.
- **ntfy notifications** — optional; silently skipped when unconfigured.

## Requirements

- Docker and Docker Compose (recommended), or Python 3.11+
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
`SLSKD_WAIT_TIMEOUT`, `UPGRADE_CHECK_INTERVAL_HOURS`.

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
```

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
