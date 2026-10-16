# spotify-slsk

Keeps a local music library in sync with your Spotify playlists by finding the
tracks on Soulseek via [`slskd`](https://github.com/slskd/slskd), tagging them,
and filing them into per-playlist folders. Runs unattended, with a small web
dashboard for adding playlists, watching progress and retrying what failed.

## Features

- **Spotify sync** — polls your playlists and picks up new tracks. Uses Spotify's
  `snapshot_id` to skip the expensive track fetch when a playlist hasn't changed.
- **Quality profile** — see [Quality profile](#quality-profile): an ordered
  list of acceptable qualities (FLAC 24-bit, FLAC 16-bit, MP3 320, ...), a
  cutoff, optional quality upgrades, required/ignored terms, and Lidarr-style
  ranking (version > quality > free slot > queue > speed > size).
- **Quality-aware search** — a fixed 320 kbps floor for MP3s
  whose bitrate is known, an effective-bitrate check from size and duration
  (`SLSKD_MIN_EFFECTIVE_MP3_KBPS`, catches 128 kbps files retagged as 320), and a
  version gate so a Live or Remix version isn't accepted for a studio track.
- **Version preference** — prefers Extended Mix over Original Mix over
  everything else, and can periodically re-search downloaded tracks for a
  better version.
- **Full tagging & cover art** — see [Library](#library): every import is
  tagged from Spotify (title, artists, album, album artist, track/disc number,
  release date, ISRC, Spotify id) with the album cover embedded, in FLAC, MP3
  and M4A. The track page previews the diff before **Re-tag**.
- **Library organisation** — a file-name template, a rename preview for
  existing files, hardlinks instead of re-downloads for tracks shared between
  playlists, a recycle bin for replaced files, and an `.m3u8` + `cover.jpg` per
  playlist folder.
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
- **Operations** — see [Operations](#operations): a System page with health
  checks (slskd, Spotify, folders, disk space, heartbeat, DB integrity...),
  scheduled tasks with "Run now", daily verified database backups and
  housekeeping; a live Queue page with cancel / cancel + blocklist.
- **Health** — `/healthz` (with the health checks under `checks`) plus a
  Docker `HEALTHCHECK` fed by a heartbeat file.
- **Notifications** — ntfy, a generic JSON webhook and a Subsonic / Navidrome
  library rescan, with per-provider event choices; all optional.
- **Optional login** — set `UI_AUTH_USERNAME` / `UI_AUTH_PASSWORD` to require
  one for every page except `/healthz`.

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
| `NTFY_URL` / `NTFY_TOPIC` | Optional ntfy notifications (also editable on Settings) |
| `UI_AUTH_USERNAME` / `UI_AUTH_PASSWORD` | Optional dashboard login (also settable on Settings) |
| `CYCLE_INTERVAL_SECONDS` | Seconds between sync cycles (default 300) |
| `UI_ENABLED` / `UI_BIND_ADDR` / `UI_PORT` | Dashboard toggle and bind (default `1`, `0.0.0.0`, `8000`) |
| `UI_SECRET_KEY` | Flask secret. Generated and stored in the DB if unset |
| `UI_COOKIE_SECURE` | Set `1` when a reverse proxy terminates TLS |
| `UI_TRUST_PROXY` | Set `1` behind one reverse proxy so the login rate limit sees the real client IP |
| `PUID` / `PGID` | UID/GID for the container (default `1000:1000`) |
| `LOGLEVEL` / `LIB_LOGLEVEL` / `TIMEZONE` | Logging verbosity and timestamps |

These are also editable from the **Settings** page, where the database value
overrides the environment variable and changes apply on the next cycle:
`MIN_MATCH_SCORE`, `SLSKD_MIN_PEER_UPLOAD_SPEED`,
`SLSKD_MIN_EFFECTIVE_MP3_KBPS`, `SLSKD_EARLY_STOP_RESPONSES`,
`SLSKD_MAX_DURATION_DIFF_SECONDS`, `SLSKD_WAIT_TIMEOUT`,
`HISTORY_RETENTION_DAYS`, `UPGRADE_CHECK_INTERVAL_HOURS`,
`LOSSLESS_CHECK_ENABLED`, `LOSSLESS_FAKE_CUTOFF_HZ`, `LOSSLESS_MIN_DROP_DB`,
`LIBRARY_SCAN_INTERVAL_HOURS`, `REQUIRED_TERMS`, `IGNORED_TERMS`,
`EMBED_COVER_ART`, `FILE_NAME_TEMPLATE`, `LINK_MODE`, `WRITE_M3U`,
`WRITE_PLAYLIST_COVER`, `RECYCLE_BIN_RETENTION_DAYS` (see [Library](#library)),
the notification settings, `UI_AUTH_USERNAME` / `UI_AUTH_PASSWORD`,
`MIN_FREE_SPACE_GB` and `BACKUP_RETENTION` (see [Operations](#operations)).
The quality profile has its own section on the same page.

`SLSKD_PREFERRED_FORMATS` is **deprecated**: on the first boot of this version
it is converted into the quality profile (below) and ignored afterwards.

## Quality profile

Every file is classified on a quality ladder, best first:

| Quality | From |
|---|---|
| FLAC 24-bit | `.flac`, bit depth ≥ 24 (any sample rate — one rung) |
| FLAC 16-bit | `.flac`, 16-bit or unknown |
| ALAC · AIFF · WAV | lossless, in that order |
| MP3 320 | `.mp3`, 320 kbps or bitrate unknown |
| MP3 V0 | VBR MP3 averaging 220-319 kbps — **always rejected** by the 320 floor |
| AAC 256+ · OGG/Opus · AAC <256 | lossy |
| MP3 <320 | **always rejected** by the 320 floor |

A lossless file that the spectrum check called `suspect` counts as MP3 320,
`fake` as MP3 <320, `upsampled`/`padded_16` as FLAC 16-bit.

The single global **profile** (Settings → Quality profile) has:

- **Allowed qualities, in order** — anything unticked is rejected with
  "quality not allowed by profile: MP3 320". The order is the ranking. The
  fixed 320 kbps MP3 floor still applies: a profile can only be stricter.
- **Cutoff** — a downloaded file at or above it is never upgraded for quality.
- **Upgrade toggle** — when on, the upgrade pass (every
  `UPGRADE_CHECK_INTERVAL_HOURS`) re-searches files below the cutoff and
  replaces one with the *same or a better version* in a *strictly better*
  quality. Better versions (Extended Mix > Original Mix > other) are always
  upgraded regardless of the toggle, but never to a lower quality; a suspect lossless file is always retried for a
  lossless copy.

Replaced files (upgrades and manual grabs) are moved to
`<playlists>/.quarantine/<playlist>/replaced/` — never deleted. Purge by hand.

**Wanted → Cutoff unmet** lists downloaded tracks below the cutoff (and suspect
lossless files); *Search selected*/*Search all* run an upgrade check on the
next cycle, ignoring the interval. Run it before enabling upgrades to see what
would be re-downloaded.

**Ranking** of acceptable candidates: version tier > profile order >
"likely upsampled" last > free upload slot > shorter queue > faster peer
(queue and speed in factor-of-two buckets) > size closest to the quality's
typical bitrate.

**Terms** — `IGNORED_TERMS` rejects any file whose full peer path (folders
included) contains a term ("ignored term: karaoke"); `REQUIRED_TERMS` rejects
files containing none of them. Comma-separated, case-insensitive substrings,
or `/regex/`. Both are empty by default. Useful ignored terms: `karaoke`,
`instrumental`, `nightcore`, `sped up`, `8d audio`, `/\bcover\b/`.

**Migration.** On first boot the profile is built from
`SLSKD_PREFERRED_FORMATS` (Settings value, else the environment, else
`flac,mp3,aiff,wav`): each format becomes its rungs in the same order (`flac` →
FLAC 24-bit, FLAC 16-bit), the cutoff is the lowest allowed quality and quality
upgrades are **off** — the same files are accepted as before and nothing
already downloaded is queued for re-download.

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

**ISRC.** Spotify's ISRC is stored per track and written into the file's tags
on import. A matching ISRC overrides a low title/artist tag score (other script
or spelling) but never a version mismatch. A *different* ISRC never rejects on
its own — singles, albums, remasters and compilations carry different ISRCs
for the same song — it is recorded in History and only adds to a rejection
that already has another cause (wrong title/artist, wrong version, duration
off). On the Library health page it is an info-only "ISRC differs" note.

**Library health.** The **Library health** page lists files already in the
library that fail the current rules: wrong song (title/artist, version),
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
hand, e.g. `find playlists/.quarantine -maxdepth 2 -type f -mtime +30 -delete`.

## Library

**Tags and cover art.** Imported files are tagged from Spotify, not from the
peer's tags: title, artists (one value per artist where the format allows it),
album, album artist, track number/total, disc number, release date, ISRC and
the Spotify track id (`SPOTIFY_TRACK_ID`), as Vorbis comments (FLAC), ID3v2.4
(MP3, AIFF) or MP4 atoms (M4A). With `EMBED_COVER_ART=1` (default) the largest
Spotify album cover is embedded; covers are cached per album under
`data/artwork/`, and a failed fetch never fails an import. On a track's page
the **Tags** section shows what is in the file next to what **Re-tag from
Spotify** would write. Databases from older versions re-read every playlist
from Spotify once after upgrading to fill in the new fields.

**File names.** `FILE_NAME_TEMPLATE` names *newly imported* files. Tokens:
`{artist} {title} {album} {album_artist} {track} {disc} {year}
{playlist_index} {isrc} {quality} {original_filename}`; numbers take a pad
width, e.g. `{track:02}` or `{playlist_index:03}`. The extension is added for
you. The default, `{original_filename}`, keeps the peer's file name exactly as
before. Names are made safe for SMB/NAS shares and Windows the way Lidarr does
it (`:` → `-`, `/` → `+`, `?` → `!`, reserved names like `CON` get a `_`, no
trailing dots or spaces, at most 200 bytes), and an existing file is never
overwritten — the new one gets ` (2)`. Settings shows an example of the
current template and rejects unknown tokens.

**Renaming existing files.** Changing the template doesn't touch files
already in the library. **Rename** (in the menu) previews old → new names per
playlist; select files, **Apply rename…**, confirm, and the daemon renames
them at the start of the next cycle — inside their playlist folder only,
skipping missing files, never overwriting.

**Tracks in several playlists.** When a track (same Spotify id, or same ISRC)
is already downloaded in another playlist, it is linked into this playlist's
folder instead of being downloaded again. `LINK_MODE=hardlink` (default) uses
no extra space and falls back to a copy when the folders are on different
filesystems; `copy` makes an independent file; `off` downloads every time.
Each playlist's file is independent: quarantining, replacing or renaming one
never affects the other. Tags are the exception — a hardlink shares its
data, so re-tagging one playlist's copy updates both (they are the same
track, so the tags are identical anyway).

**Recycle bin.** Files replaced by an upgrade or a manual grab go to
`.quarantine/<playlist>/replaced/`. They are kept forever by default; set
`RECYCLE_BIN_RETENTION_DAYS` to have a daily step delete those older than that
many days (counted from when they were replaced). Files you quarantined from
Library health (`.quarantine/<playlist>/` itself) are **never** deleted
automatically.

**Playlist files.** Each playlist folder gets `<playlist name>.m3u8` (UTF-8,
Spotify order, downloaded tracks only, relative paths — importable by Navidrome,
Jellyfin, Plex or any player) and `cover.jpg` from the Spotify playlist image.
Turn them off with `WRITE_M3U=0` / `WRITE_PLAYLIST_COVER=0`. Your own files
are never overwritten: a cover you placed is left alone, and if a file with
the playlist's m3u8 name is yours, ours is written as `<name> (spotify-slsk).m3u8`.

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

## Operations

**System → Status** lists health checks (each `ok`, `warn` or `error`), the
version, uptime, schema version, database size, free disk space and the
backups. The same checks are in `/healthz` under `checks`; `/healthz`'s status
code still depends only on the schema version and the daemon heartbeat, so
Docker's `HEALTHCHECK` and any monitor keyed on it behave as before.

| Check | Warns / fails when |
|---|---|
| `slskd` | slskd unreachable, not connected or not logged in (checked by the daemon every 5 min) |
| `spotify` | no credentials, or Spotify rejects them |
| `downloads_dir`, `playlists_dir` | missing or not writable by the container user |
| `disk_space` | less than `MIN_FREE_SPACE_GB` (default 5) free on the playlists volume |
| `heartbeat` | the daemon loop has not touched its heartbeat in `HEARTBEAT_STALE_SECONDS` |
| `last_cycle` | no cycle completed for 48 h |
| `flac_binary` | `flac` missing while the lossless check is on |
| `db_integrity` | the daily `PRAGMA quick_check` found a problem |
| `ui_auth` | the UI listens beyond loopback with no login (inside Docker it always listens on `0.0.0.0`: fine if the port is published on `127.0.0.1` only) |

**System → Tasks** shows each periodic task — Spotify sync, library scan,
quality backfill, recycle-bin purge, database backup, housekeeping, integrity
check, health checks — with its interval, last run, duration, result and next
run. **Run now** queues a task for the daemon, which picks it up at the start of
the next cycle or between two playlists; clicking again while it is queued does
nothing.

**Queue** shows slskd's current downloads (progress, speed, ETA) with the
track each one is for, plus requests waiting for the daemon (manual grabs,
quarantines, renames, upgrade checks). **Cancel** stops a transfer; the track
counts it as a failed attempt for that file. **Cancel + blocklist** also adds
the file to the blocklist so no track downloads it again.

**Housekeeping** runs daily: trims history, compacts the database when much of
it is free space, and removes expired artwork cache entries and interrupted
temp files of its own. Partial downloads older than 7 days in slskd's
`incomplete/` folder are reported in the task result, never deleted.

The library health scan now also advances a little (50 files) after every
playlist, so a long download cycle no longer holds it up for hours.

### Notifications

Configure on **Settings → Notifications**; each provider has a test button.

| Event | When |
|---|---|
| `grabbed`, `imported`, `upgraded` | a file was downloaded, imported, or replaced by a better one |
| `download_failed`, `import_rejected` | a transfer failed; a file was rejected after download (fake lossless included) |
| `quarantined` | a library file was quarantined from Library health |
| `playlist_synced` | a sync found new tracks in a playlist |
| `cycle_summary` | a cycle added, downloaded or failed something |
| `health_issue`, `health_restored` | a health check changed status (only on change, also across restarts) |

Per-track events are batched: one message per event and playlist per cycle
("Imported: 12 track(s) in Chill"), listing up to 25 tracks.

- **ntfy**: `NTFY_URL` (e.g. `https://ntfy.sh`) and `NTFY_TOPIC`.
  `NTFY_EVENTS` defaults to `playlist_synced,cycle_summary,health_issue,health_restored`.
- **Webhook**: `WEBHOOK_URL` receives `POST` JSON `{"event": ..., "ts": "<ISO 8601>", "data": {...}}`
  for each event in `WEBHOOK_EVENTS` (default: all). For a secret, set
  `WEBHOOK_HEADER_NAME` (e.g. `Authorization`) and `WEBHOOK_HEADER_VALUE`
  (e.g. `Bearer …`). Requests time out after 10 s and do not follow redirects.
- **Navidrome / Subsonic**: `SUBSONIC_URL` (e.g. `http://navidrome:4533`),
  `SUBSONIC_USERNAME`, `SUBSONIC_PASSWORD`. After a cycle that imported or
  upgraded something, the server is asked once to rescan its library
  (`/rest/startScan`, salted-token auth, so the password is never sent). In
  Navidrome the user must be an admin.

A provider that fails is paused (1 minute, doubling up to an hour) and its
events are dropped meanwhile. `WEBHOOK_HEADER_VALUE` and `SUBSONIC_PASSWORD`
are never shown again after saving (leave the field blank to keep them, tick
"Clear" to remove) and never written to the logs; neither is any notification
URL, since those often carry tokens.

The old per-playlist "Finished processing" and startup ntfy messages are gone;
`cycle_summary` replaces the former and is only sent when something happened.

## Security

**The dashboard has no authentication by default.** Compose binds it to
`127.0.0.1` for that reason. To expose it beyond loopback, either turn on the
built-in login or put a reverse proxy with authentication in front of it, and
set `UI_COOKIE_SECURE=1` if a proxy terminates TLS.

**Built-in login**: set `UI_AUTH_USERNAME` and `UI_AUTH_PASSWORD` in `.env`, or
on **Settings → System**. Every page then requires a login except `/healthz`
(so Docker and monitors keep working). The password is stored only as a salted
hash (a password saved on Settings replaces the one from `.env`), five failed
logins from one address lock it out for 15 minutes (behind a reverse proxy,
set `UI_TRUST_PROXY=1` so the client address comes from `X-Forwarded-For`;
only with a proxy really in front), and changing the password
logs every other session out. Locked out? Stop the container and run
`sqlite3 data/playlist_tracks.db "DELETE FROM settings WHERE key IN ('UI_AUTH_USERNAME','_ui_auth_password_hash')"`,
then remove the variables from `.env`.

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

The database is backed up daily to
`data/backups/playlist_tracks-YYYYMMDD-HHMMSS.db` using SQLite's online backup
API (safe while everything is running), and each backup is integrity-checked
before it is kept. `BACKUP_RETENTION` (default 7) is how many are kept; `0`
turns the daily backup off. **Backup now** on System → Status takes one
immediately. Backups contain every setting, **secrets included** (webhook
header, Subsonic password, the login hash, the session key), so the folder is
created `0700` and each file `0600`; keep it that way when you copy them
elsewhere. Backups are single self-contained files; only files named like
the above are ever deleted. They cover the database (playlists, track states,
settings, history), not your music: back up the playlists folder separately.

**Restore**:

```bash
docker compose stop spotify-slsk
cp data/playlist_tracks.db data/playlist_tracks.db.before-restore   # just in case
cp data/backups/playlist_tracks-20260101-030000.db data/playlist_tracks.db
rm -f data/playlist_tracks.db-wal data/playlist_tracks.db-shm
docker compose start spotify-slsk
```

Delete the `-wal` and `-shm` files when restoring: they belong to the database
you replaced. To copy the live database by hand instead, copy all three files,
or run `PRAGMA wal_checkpoint(TRUNCATE);` first.

## Releases & images

Images are published to `ghcr.io/guilhermewolf/spotify-slsk/spotify-slsk`
for `linux/amd64` and `linux/arm64`, each with an SBOM, SLSA provenance and a
signed GitHub attestation. Nothing is published unless CI (the full
`make check`, the container smoke test and a Trivy scan) passed for that exact
commit.

| Tag | Moves when | Use it for |
| --- | --- | --- |
| `:latest`, `:X`, `:X.Y`, `:X.Y.Z` | a stable release `vX.Y.Z` is published | **production** |
| `:beta` | an RC (`vX.Y.Z-rc.N`) *or* a stable release is published | testers — never older than `:latest` |
| `:X.Y.Z-rc.N` | that RC is published | pinning a specific RC |
| `:edge`, `:main` | every commit on `main` that passes CI | bleeding edge |
| `:sha-<short>` | every published build | pinning an exact build |

**`:latest` no longer tracks `main`.** It only moves when a release is
published. A deployment on `:latest` stays on the last image `main` pushed
before this change until the first release-please release is merged; switch
to `:edge` if you want every commit.

Check what you are running on **System → Status** (the version comes from
`spotify_slsk/__init__.py`), and verify an image's provenance with
`gh attestation verify oci://ghcr.io/guilhermewolf/spotify-slsk/spotify-slsk:latest -R guilhermewolf/spotify-slsk`.

### Commit messages

Releases are automated by [release-please](https://github.com/googleapis/release-please)
from [Conventional Commits](https://www.conventionalcommits.org/) on `main`:
`feat:` bumps the minor version, `fix:`/`perf:` the patch, and a `!` after the
type (`feat!:`) or a `BREAKING CHANGE:` footer the major. `feat`, `fix`,
`perf`, `refactor`, `docs` and `ci` appear in `CHANGELOG.md`; `chore`, `test`,
`build` and `style` are hidden. PR titles become squash-merge commit messages,
so give them the same form.

### Cutting a stable release

Every push to `main` updates an open **release PR** ("chore(main): release
X.Y.Z") with the next version, the changelog and the bumped
`__version__`. Merge it when you want to ship: that run tags `vX.Y.Z`,
creates the GitHub release from the changelog, and publishes
`:X.Y.Z :X.Y :X :latest :beta`.

### Cutting a release candidate

Either let release-please do it, so the version in the image and changelog
match (preferred):

```bash
git commit --allow-empty -m "chore: release 3.0.0-rc.1" -m "Release-As: 3.0.0-rc.1"
# push/merge to main, then merge the release PR it opens
```

or push a tag on a commit that is already on `main` (quick, but the app still
reports the previous version):

```bash
git tag v3.0.0-rc.1 <sha> && git push origin v3.0.0-rc.1
```

Both publish `:3.0.0-rc.1` and `:beta` and mark the GitHub release as a
pre-release; `:latest` does not move. To go final after an RC, use
`Release-As: 3.0.0` the same way, or the next release PR will compute its
version from the RC.
