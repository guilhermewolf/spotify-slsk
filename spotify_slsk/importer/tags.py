"""Reading and writing audio tags, and reading the real audio length.

One tag writer (`write_tags`) serves both the daemon (via the non-raising
`tag_audio_file`) and the web UI's re-tag action.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TSRC
from mutagen.mp3 import MP3


def audio_length_seconds(file_path: str) -> float | None:
    """Actual audio duration via mutagen, or None if unreadable."""
    try:
        mf = MutagenFile(file_path)
        if mf is not None and mf.info is not None and mf.info.length:
            return float(mf.info.length)
    except Exception as e:
        logging.debug(f"Could not read duration of {file_path}: {e}")
    return None


def normalize_isrc(value: Any) -> str | None:
    """Canonical 12-character ISRC ("GB-AYE-06-01498" -> "GBAYE0601498"),
    or None when the value is not one."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return cleaned if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", cleaned) else None


def read_isrc(file_path: str) -> str | None:
    """The file's ISRC tag (ID3 TSRC, Vorbis ISRC, MP4 iTunes ISRC), or None.
    Never raises."""
    try:
        tags = MutagenFile(file_path).tags
    except Exception as e:
        logging.debug(f"Could not read ISRC from {file_path}: {e}")
        return None
    if tags is None:
        return None
    if hasattr(tags, "getall"):  # ID3 (mp3, aiff, wav)
        frames = tags.getall("TSRC")
        values = frames[0].text if frames else []
    else:  # Vorbis comments (case-insensitive) or MP4 freeform
        values = tags.get("isrc") or tags.get("----:com.apple.iTunes:ISRC") or []
    return normalize_isrc(values[0]) if values else None


def extract_metadata_from_file(file_path: str) -> tuple[str | None, str | None, str | None]:
    # mutagen's per-format classes share no useful static type.
    audio: Any
    try:
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".mp3":
            audio = MP3(file_path, ID3=ID3)
            title = audio.tags.get("TIT2")
            artist = audio.tags.get("TPE1")
            album = audio.tags.get("TALB")

            title = title.text[0] if title else None
            artist = artist.text[0] if artist else None
            album = album.text[0] if album else None

        elif ext == ".flac":
            audio = FLAC(file_path)
            title = audio.get("title", [None])[0]
            artist = audio.get("artist", [None])[0]
            album = audio.get("album", [None])[0]

        elif ext == ".aiff":
            audio = AIFF(file_path)
            title = audio.get("TIT2", [None])[0]
            artist = audio.get("TPE1", [None])[0]
            album = audio.get("TALB", [None])[0]

        elif ext == ".wav":
            logging.warning("WAV format may not have embedded metadata.")
            return None, None, None

        else:
            logging.warning(f"Unsupported audio format for metadata: {ext}")
            return None, None, None

        logging.info(f"Extracted metadata from file: {file_path} - Title: {title}, Artist: {artist}, Album: {album}")
        return title, artist, album

    except Exception as e:
        logging.error(f"Error reading metadata from {file_path}: {e}")
        return None, None, None


def write_tags(
    path: str, title: str | None, artist: str | None, album: str | None,
    isrc: str | None = None,
) -> None:
    """Write title/artist/album (and the ISRC when known; an existing one is
    left alone otherwise). Supports mp3, flac and aiff.

    Raises ValueError for any other extension and lets mutagen errors
    propagate - the web UI's re-tag action reports them to the operator.
    The daemon goes through `tag_audio_file`, which logs instead.
    """
    title = title or ""
    artist = artist or ""
    album = album or ""
    ext = os.path.splitext(path)[1].lower()
    audio: Any

    if ext == ".mp3":
        audio = MP3(path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.tags.add(TALB(encoding=3, text=album))
        if isrc:
            audio.tags.add(TSRC(encoding=3, text=isrc))
        audio.save()
    elif ext == ".flac":
        audio = FLAC(path)
        audio["title"] = title
        audio["artist"] = artist
        audio["album"] = album
        if isrc:
            audio["isrc"] = isrc
        audio.save()
    elif ext == ".aiff":
        audio = AIFF(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.tags.add(TALB(encoding=3, text=album))
        if isrc:
            audio.tags.add(TSRC(encoding=3, text=isrc))
        audio.save()
    else:
        raise ValueError(f"Unsupported extension for retag: {ext}")


def tag_audio_file(
    file_path: str, title: str | None, artist: str | None, album: str | None,
    isrc: str | None = None,
) -> bool:
    """Daemon-side tagging: never raises. WAV is skipped (counts as done),
    other unsupported formats and mutagen errors log and return False."""
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == '.wav':
            logging.warning("WAV tagging is not fully supported; skipping tags.")
        elif ext not in ('.mp3', '.flac', '.aiff'):
            logging.warning(f"Unsupported format for tagging: {ext}")
            return False
        else:
            write_tags(file_path, title, artist, album, isrc)

        logging.info(f"Tagged {file_path} successfully.")
        return True

    except Exception as e:
        logging.error(f"Failed to tag {file_path}: {e}")
        return False
