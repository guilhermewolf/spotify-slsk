# spotify-sldl

## Overview

The Spotify Playlist Downloader is a Python-based tool designed to continuously monitor Spotify playlists, download tracks using the `sldl` command-line tool, and manage the downloaded tracks on your filesystem. The tool also integrates with NTFY.sh to send notifications when a playlist has been fully downloaded.

## Features

- **Continuous Monitoring**: The tool runs 24/7, checking Spotify playlists for new tracks.
- **Track Downloading**: Automatically downloads tracks using `sldl` based on the track's metadata.
- **Startup Check**: On startup, the tool checks existing tracks on your filesystem, updates the database, and cleans up untracked files.
- **Duplicate Prevention**: Tracks are matched using metadata to avoid downloading duplicates.
- **NTFY.sh Notifications**: Sends notifications when a playlist has been fully downloaded, including fun and informative emojis.
