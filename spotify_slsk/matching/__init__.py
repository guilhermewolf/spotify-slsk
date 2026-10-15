"""Deciding whether a file is the track we want.

Two phases, deliberately kept apart:

* ``candidates`` - **search phase**. Judges a peer's *file name* (plus the
  slskd-reported format, bitrate, length) before anything is downloaded.
  Strict, rule-based: every expected title word in the basename, the whole
  artist name in the name or its folders, identical version keywords.
* ``tracks`` - **local phase**. Judges a downloaded/on-disk file's *tags*
  against DB rows with difflib similarity. ``score_track_match`` gates a
  destructive accept/delete; ``_looks_like_match`` is a lenient check used
  only by the non-destructive startup reconcile.

``normalize`` holds what both phases share: accent/punctuation folding,
artist-credit splitting and the version-keyword sets. Merging the phases'
scoring would trade one risk profile for the other, so they stay separate.
"""
