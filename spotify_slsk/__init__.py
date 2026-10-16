"""spotify-slsk — sync Spotify playlists into a local library via slskd.

The daemon entrypoint is `spotify_slsk.daemon.main`, reachable as
`python -m spotify_slsk`. Kept import-light on purpose: importing this package
must not pull in spotipy, mutagen or slskd_api, so tests and tooling can
import a single submodule cheaply.
"""

# Single source of the version: pyproject.toml reads it, /system/status shows
# it, and release-please bumps it (the annotation below) in each release PR.
__version__ = "2.0.0"  # x-release-please-version
