"""Domain types shared across packages."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

# One peer file judged by matching.candidates.evaluate_candidates: the slskd
# fields we use plus "accepted"/"reason". A plain dict on purpose - it is
# stored in runtime_state, rendered by templates and carried into history.
Candidate = dict[str, Any]

# is_blocked(username, filename) - see db.load_blocklist.
IsBlocked = Callable[[str | None, str | None], bool]


class Track:
    def __init__(self, id, name, artist, album, playlist_id, duration_ms=None, isrc=None):
        self.id = id
        self.name = name
        self.artist = artist
        self.album = album
        self.playlist_id = playlist_id
        self.duration_ms = duration_ms
        self.isrc = isrc

    def __repr__(self):
        return f"Track(id={self.id}, name={self.name}, artist={self.artist}, album={self.album})"
