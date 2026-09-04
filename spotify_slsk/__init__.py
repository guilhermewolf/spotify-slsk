"""spotify-slsk — sync Spotify playlists into a local library via slskd.

The daemon entrypoint is `spotify_slsk.app.main`, reachable as
`python -m spotify_slsk`. Kept import-light on purpose: importing this package
must not pull in spotipy, mutagen or slskd_api, so tests and tooling can
import a single submodule cheaply.
"""
