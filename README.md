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
- **Validation** — see [Validation](#validation): fake-lossless detection
  (FLAC/WAV that is really a lossy transcode is rejected and
  re-searched), ISRC checks against Spotify, implausibly small FLACs skipped
  before download, and a library health scan with quarantine & re-download.
- **Retry and back-off** — a track that fails twice is suspended for two days
  rather than retried forever; rejected filenames are remembered so the same bad
  file isn't downloaded again.
- **Blocklist** — peer files that slskd reports as failed/rejected, or that
  turn out broken after download (no tags, unreadable, truncated, padded
  16-bit), are skipped
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
`HISTORY_RETENTION_DAYS`, `UPGRADE_CHECK_INTERVAL_HOURS`,
`LOSSLESS_CHECK_ENABLED`, `LOSSLESS_FAKE_CUTOFF_HZ`, `LOSSLESS_MIN_DROP_DB`,
`LIBRARY_SCAN_INTERVAL_HOURS`.

## Validation

**Fake lossless.** Every FLAC/WAV download is spectrum-checked before it is
tagged and moved (~0.2-2 s: three 10 s windows from the middle of the track,
decoded with the `flac` CLI that ships in the image). A lossy encoder cuts the
top of the spectrum with a steep lowpass (128 kbps MP3 ~16 kHz, 192 ~19 kHz,
320 ~20 kHz), and transcoding to FLAC keeps the hole. Verdicts:

| Verdict | Rule | Effect |
|---|---|---|
| `fake` | drop ≥ `LOSSLESS_MIN_DROP_DB` (15 dB) below `LOSSLESS_FAKE_CUTOFF_HZ` (19500 Hz), in every sampled window, cutoffs within 1 kHz (otherwise `suspect`) | rejected and deleted; skipped for this track only |
| `upsampled` | > 48 kHz file with a ≥ 20 dB drop at ≤ 26 kHz | rejected (a CD master resampled to "hi-res"); this track only |
| `padded_16` | 24-bit file whose low 8 bits are always zero | rejected, peer file blocklisted |
| `suspect` | the drop sits between the cutoff and 20.5 kHz | **kept**, flagged, and retried by the upgrade pass for a lossless copy |
| `ok` / `unknown` | no cliff / could not decode | kept (`unknown` never rejects) |

Limits, measured on real and synthetic files: margins between real masters and
transcodes are only ~3-5 dB, so the thresholds are settings. MP3-320 is
indistinguishable from a brickwalled master and is only `suspect`; a
high-bitrate AAC (≥ 256 kbps) transcode keeps content to ~20 kHz and is **not
detectable** this way. Lower `LOSSLESS_MIN_DROP_DB` to catch more, at the risk
of flagging dark-sounding masters. The suspect retry needs
`UPGRADE_CHECK_INTERVAL_HOURS` > 0. The check also applies to manual grabs.

Before downloading, a 16-bit FLAC whose size/length works out below 400 kbps
is rejected, and a 24-bit ≥ 88.2 kHz FLAC below 1200 kbps is ranked last as
"likely upsampled" (both visible in the interactive search table).

**ISRC.** Spotify's ISRC is stored per track. On import, a file whose ISRC tag
differs is rejected as the wrong song (for that track only); a matching ISRC
overrides a low title/artist tag score (other script or spelling) but never a
version mismatch. Imports write the ISRC into the file's tags. Re-releases can
carry a different ISRC for the same song; check History for "ISRC mismatch" if
a track keeps failing.

**Library health.** The **Library health** page lists files already in the
library that fail the current rules: wrong song (title/artist, version, ISRC),
duration off, fake or suspect lossless, missing file — with the evidence
(expected vs file tags, durations, cutoff). Press **Scan library** (or set
`LIBRARY_SCAN_INTERVAL_HOURS`); the daemon checks up to 200 files / 120 s per
cycle and only re-checks files that changed. For each finding:

- **Quarantine & re-download** (per row or for the selected rows) moves the
  file to `<playlists>/.quarantine/<playlist>/` — never deleted — puts the
  track back to pending and skips that filename next time. The next cycle
  downloads it again.
- **Ignore** marks a false positive; it is not flagged again unless the file
  changes.

Quarantined files are never purged automatically. Listen, then delete them by
hand, e.g. `find playlists/.quarantine -type f -mtime +30 -delete`.

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
python3.12 -m venv .venv
make install PYTHON=.venv/bin/python
make check   PYTHON=.venv/bin/python

# run the daemon outside Docker (needs a .env and a reachable slskd)
.venv/bin/python -m spotify_slsk
```

The application lives in the `spotify_slsk/` package (`daemon.py` is the
entrypoint; `spotify/`, `slskd/`, `matching/`, `importer/`, `library/`, `db/` and
`webui/` hold the rest — CLAUDE.md has the map); `tests/` sits alongside it. Run from the repo root — the SQLite path is resolved relative to the
working directory.

`make check` runs the same compile, lint (ruff), typecheck (mypy), test and
dependency-audit steps as CI, on Python 3.12. The test suite is hermetic: `tests/conftest.py` forces fake credentials, an
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
