# spotify-slsk

## Overview

**spotify-slsk** is a Python-based automation tool that monitors Spotify playlists, searches for matching tracks on Soulseek using the `slskd` API, downloads them based on audio format preferences, and tags them with accurate metadata. It is designed for 24/7 unattended operation and supports error handling, retry logic, and detailed notifications via [ntfy.sh](https://ntfy.sh).

---

## Features

- **🎵 Spotify Integration**: Automatically syncs with public Spotify playlists to track new songs.
- **🔎 Smart Searching**: Uses the `slskd` API to find matching tracks on Soulseek, with fuzzy matching for best-fit candidates.
- **⬇️ Controlled Downloads**: Only downloads one file per track at a time; retries once on failure with the next best match.
- **🎯 Format Prioritisation**: Filters search results to prefer specific audio formats (e.g., `.flac`, `.mp3`, `.aiff`) via environment variable.
- **🧠 Metadata Tagging**: Automatically tags downloaded files with title, artist, and album metadata using `mutagen`.
- **🧹 Startup Cleanup**: On startup, it verifies existing files, cleans invalid ones, and updates download status.
- **📂 Organised Storage**: Moves successfully downloaded and tagged files into dedicated per-playlist folders.
- **📊 SQLite Persistence**: Keeps track of track status, download attempts, and previously tried files to avoid duplication.
- **🔁 Retry & Suspension Logic**: Skips tracks that failed too many times and retries them after a cooling-off period.
- **📲 ntfy.sh Integration**: Sends rich notifications on start, playlist updates, and download completions.

---

## Prerequisites

- Python 3.9+
- Running instance of [`slskd`](https://github.com/slskd/slskd)
- Soulseek account credentials
- Spotify API credentials (client ID/secret)
- Docker (optional, but recommended)

---

## Environment Variables

| Variable                  | Description                                                   |
|---------------------------|---------------------------------------------------------------|
| `SPOTIPY_CLIENT_ID`       | Your Spotify API client ID                                    |
| `SPOTIPY_CLIENT_SECRET`   | Your Spotify API client secret                                |
| `SPOTIFY_PLAYLIST_URLS`   | Comma-separated list of Spotify playlist URLs to monitor      |
| `SLSKD_HOST_URL`          | Base URL to your slskd instance (e.g. `http://slskd:5030`)    |
| `SLSKD_API_KEY`           | API key configured in `slskd.yml`                             |
| `SLSKD_PREFERRED_FORMATS` | Preferred audio formats (e.g., `.flac,.mp3,.aiff`)            |
| `NTFY_URL`                | ntfy.sh base URL                                              |
| `NTFY_TOPIC`              | Topic name for sending notifications                          |
| `DOWNLOAD_ROOT`           | Directory where files are downloaded (default: `/downloads`)  |
| `DATA_ROOT`               | Directory where verified files are moved (default: `/data`)   |

---

## Running

### 1. Clone the repo

```bash
git clone https://github.com/your-username/spotify-slsk.git
cd spotify-slsk
```

### 2. Set up your .env

```bash
SPOTIPY_CLIENT_ID=your_spotify_client_id
SPOTIPY_CLIENT_SECRET=your_spotify_client_secret
SPOTIFY_PLAYLIST_URLS=https://open.spotify.com/playlist/...
SLSKD_HOST_URL=http://slskd:5030
SLSKD_API_KEY=your_api_key
SLSKD_PREFERRED_FORMATS=.flac,.mp3,.aiff
NTFY_URL=https://ntfy.sh
NTFY_TOPIC=spotify-downloads
```

### 3. Run with Docker Compose
````bash
docker-compose
```