"""File naming for imported tracks: FILE_NAME_TEMPLATE -> a safe basename.

Tokens (`{name}`, numeric ones take a zero-pad width: `{track:02}`):

    {artist} {title} {album} {album_artist} {track} {disc} {year}
    {playlist_index} {isrc} {quality} {original_filename}

The extension is implied: it is always the downloaded file's own. The
default, "{original_filename}", keeps the peer's basename untouched - the
behaviour before templates existed.

Rendered names are sanitised like Lidarr's FileNameBuilder.CleanFileName,
so they are safe on SMB/NAS shares and Windows clients: illegal characters
replaced (`: ` -> ` - `, `:` -> `-`, `/` `\\` -> `+`, `?` -> `!`, `*` -> `-`,
`<>|"` and control characters dropped), reserved device names (CON, NUL,
COM1...) suffixed with `_`, leading/trailing dots and spaces trimmed, and
the whole basename capped at MAX_NAME_BYTES of UTF-8.
"""
from __future__ import annotations

import os
import re
from typing import Any

from ..matching.quality import describe

DEFAULT_TEMPLATE = "{original_filename}"
TEXT_TOKENS = ("artist", "title", "album", "album_artist", "isrc", "quality", "original_filename")
NUMBER_TOKENS = ("track", "disc", "year", "playlist_index")
TOKENS = TEXT_TOKENS + NUMBER_TOKENS
MAX_NAME_BYTES = 200

_TOKEN_RE = re.compile(r"\{(\w+)(?::(\d{1,2}))?\}")
_RESERVED_RE = re.compile(r"^(aux|com[1-9]|con|lpt[1-9]|nul|prn)(?=\.|$)", re.IGNORECASE)
_ILLEGAL = str.maketrans({
    "\\": "+", "/": "+", "<": "", ">": "", "?": "!", "*": "-", "|": "", '"': "",
    **{chr(c): "" for c in range(32)}, "\x7f": "",
})

# The sample the settings page renders the template with.
EXAMPLE_ROW = {
    "name": "Desires Ignite - Radio Edit", "artists": "Reber", "album": "Desires Ignite",
    "album_artist": "Reber", "track_number": 2, "disc_number": 1,
    "release_date": "2021-03-05", "position": 7, "isrc": "GBAYE0601498",
}
EXAMPLE_ORIGINAL = "03 - reber - desires ignite (radio edit).flac"


def validate_template(template: str) -> str | None:
    """An error message, or None when the template is usable."""
    if not template.strip():
        return "must not be empty"
    if "/" in template or "\\" in template:
        return "must be a file name, not a path (no / or \\)"
    for name, width in _TOKEN_RE.findall(template):
        if name not in TOKENS:
            return f"unknown token {{{name}}}; use one of " + " ".join(f"{{{t}}}" for t in TOKENS)
        if width and name not in NUMBER_TOKENS:
            return f"{{{name}}} is text; only {', '.join(NUMBER_TOKENS)} take a :NN width"
    if "{" in _TOKEN_RE.sub("", template) or "}" in _TOKEN_RE.sub("", template):
        return "unbalanced { or }"
    if not _TOKEN_RE.search(template):
        return "must contain at least one {token}"
    return None


def clean(text: str) -> str:
    """Lidarr's CleanFileName with ReplaceIllegalCharacters + Smart colons."""
    text = text.replace(": ", " - ").replace(":", "-").translate(_ILLEGAL)
    return text


def _values(row: dict[str, Any], original_filename: str, quality: str | None) -> dict[str, Any]:
    date = row.get("release_date") or ""
    return {
        "artist": row.get("artists"), "title": row.get("name"), "album": row.get("album"),
        "album_artist": row.get("album_artist") or row.get("artists"),
        "isrc": row.get("isrc"),
        "quality": describe(quality) if quality else None,
        "original_filename": os.path.splitext(original_filename)[0],
        "track": row.get("track_number"), "disc": row.get("disc_number"),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "playlist_index": row.get("position"),
    }


def _truncate(stem: str, ext: str) -> str:
    budget = MAX_NAME_BYTES - len(ext.encode())
    data = stem.encode()
    if len(data) <= budget:
        return stem
    return data[:budget].decode("utf-8", "ignore").rstrip(" .")


def file_name(
    template: str, row: dict[str, Any], original_filename: str, quality: str | None = None,
) -> str:
    """The basename (with extension) for `row`'s file. Unknown values render
    empty; an empty result falls back to the original name."""
    ext = os.path.splitext(original_filename)[1]
    if template == DEFAULT_TEMPLATE or validate_template(template):
        return original_filename
    values = _values(row, original_filename, quality)

    def sub(m: re.Match[str]) -> str:
        value = values.get(m.group(1))
        if value is None or value == "":
            return ""
        if m.group(2) and isinstance(value, int):
            return f"{value:0{int(m.group(2))}d}"
        return clean(str(value))

    stem = _TOKEN_RE.sub(sub, template)
    # Empty tokens leave dangling separators: "A -  - T", "A - ", "(  )".
    stem = re.sub(r"\(\s*\)|\[\s*\]", "", stem)
    stem = re.sub(r"(\s+-)(\s+-)+\s", r"\1 ", stem)
    return safe_name(stem.strip(" -_"), ext) or original_filename


def safe_name(stem: str, ext: str) -> str:
    """`stem + ext` made safe for any share (see the module docstring), or
    "" when nothing usable is left."""
    stem = " ".join(clean(stem).split()).strip(" .")
    stem = _RESERVED_RE.sub(r"\1_", stem)
    return _truncate(stem, ext) + ext if stem else ""


def example(template: str) -> str:
    return file_name(template, EXAMPLE_ROW, EXAMPLE_ORIGINAL, "FLAC_16")
