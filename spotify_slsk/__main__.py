"""Entrypoint for `python -m spotify_slsk`.

The daemon resolves its SQLite path relative to the working directory
("./data/playlist_tracks.db"), so run this from the directory holding `data/`
— which is what the container does (WORKDIR /app).
"""
from spotify_slsk.app import main

if __name__ == "__main__":
    main()
