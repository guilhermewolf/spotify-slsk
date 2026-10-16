"""Reading and writing audio tags, and reading the real audio length.

One tagger serves the daemon (the non-raising `tag_audio_file`), the web
UI's re-tag action (`write_tags`) and its preview (`read_tags` vs
`spotify_tags`). Tags are a format-neutral dict of lists: `spotify_tags`
builds it from a DB row, `write_tags` maps it onto Vorbis comments (FLAC),
ID3v2.4 (MP3, AIFF) or MP4 atoms (M4A), `read_tags` maps a file back.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TSRC, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm


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


# The neutral tag keys, in display order. "track" is "n/total", "disc" "n".
TAG_KEYS = (
    "title", "artist", "album", "album_artist", "track", "disc", "date",
    "isrc", "spotify_track_id",
)
TAG_LABELS = {
    "title": "Title", "artist": "Artist(s)", "album": "Album",
    "album_artist": "Album artist", "track": "Track", "disc": "Disc",
    "date": "Release date", "isrc": "ISRC", "spotify_track_id": "Spotify track id",
}
TAGGABLE_EXTS = (".mp3", ".flac", ".aiff", ".m4a")

Tags = dict[str, list[str]]

_VORBIS = {
    "title": "title", "artist": "artist", "album": "album",
    "album_artist": "albumartist", "disc": "discnumber", "date": "date",
    "isrc": "isrc", "spotify_track_id": "spotify_track_id",
}
_ID3 = {
    "title": TIT2, "artist": TPE1, "album": TALB, "album_artist": TPE2,
    "track": TRCK, "disc": TPOS, "date": TDRC, "isrc": TSRC,
}
_MP4 = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "album_artist": "aART", "date": "\xa9day"}
_MP4_FREEFORM = {"isrc": "----:com.apple.iTunes:ISRC", "spotify_track_id": "----:com.apple.iTunes:SPOTIFY_TRACK_ID"}
_SPOTIFY_ID_TXXX = "SPOTIFY_TRACK_ID"


def spotify_tags(row: dict[str, Any], isrc: str | None = None) -> Tags:
    """What the library file of a DB track row should carry. Unknown fields
    are left out, so writing never blanks a tag Spotify didn't supply.
    `isrc` overrides the row's (None: the row's; "" : leave the file's)."""
    artists = row.get("artist_list") or ([row["artists"]] if row.get("artists") else [])
    number, total = row.get("track_number"), row.get("track_total")
    track = f"{number}/{total}" if number and total else (str(number) if number else "")
    values = {
        "title": [row.get("name") or ""],
        "artist": artists,
        "album": [row.get("album") or ""],
        "album_artist": [row.get("album_artist") or ""],
        "track": [track],
        "disc": [str(row["disc_number"]) if row.get("disc_number") else ""],
        "date": [row.get("release_date") or ""],
        "isrc": [normalize_isrc(row.get("isrc") if isrc is None else isrc) or ""],
        "spotify_track_id": [row.get("id") or ""],
    }
    return {k: v for k, v in values.items() if any(v)}


def _id3_write(tags_obj: Any, tags: Tags) -> None:
    for key, frame in _ID3.items():
        if key in tags:
            tags_obj.delall(frame.__name__)
            tags_obj.add(frame(encoding=3, text=tags[key]))
    if "spotify_track_id" in tags:
        tags_obj.delall(f"TXXX:{_SPOTIFY_ID_TXXX}")
        tags_obj.add(TXXX(encoding=3, desc=_SPOTIFY_ID_TXXX, text=tags["spotify_track_id"]))


def _image_mime(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


def write_tags(path: str, tags: Tags, cover: bytes | None = None) -> None:
    """Write `tags` (see spotify_tags) and, when given, `cover` as the
    front-cover picture (replacing any existing one). Keys absent from
    `tags` are left as the file has them.

    Raises ValueError for an unsupported extension and lets mutagen errors
    propagate - the web UI's re-tag action reports them to the operator.
    The daemon goes through `tag_audio_file`, which logs instead.
    """
    ext = os.path.splitext(path)[1].lower()
    audio: Any
    if ext in (".mp3", ".aiff"):
        audio = MP3(path, ID3=ID3) if ext == ".mp3" else AIFF(path)
        if audio.tags is None:
            audio.add_tags()
        _id3_write(audio.tags, tags)
        if cover:
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime=_image_mime(cover), type=3, desc="Cover", data=cover))
        audio.save(v2_version=4)
    elif ext == ".flac":
        audio = FLAC(path)
        for key, name in _VORBIS.items():
            if key in tags:
                audio[name] = tags[key]
        if "track" in tags:
            number, _, total = tags["track"][0].partition("/")
            audio["tracknumber"] = number
            if total:
                audio["tracktotal"] = total
        if cover:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, _image_mime(cover), "Cover", cover
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()
    elif ext == ".m4a":
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        for key, atom in _MP4.items():
            if key in tags:
                audio.tags[atom] = tags[key]
        for key, atom in _MP4_FREEFORM.items():
            if key in tags:
                audio.tags[atom] = [MP4FreeForm(v.encode()) for v in tags[key]]
        if "track" in tags:
            number, _, total = tags["track"][0].partition("/")
            audio.tags["trkn"] = [(int(number), int(total or 0))]
        if "disc" in tags:
            audio.tags["disk"] = [(int(tags["disc"][0]), 0)]
        if cover:
            fmt = MP4Cover.FORMAT_PNG if cover.startswith(b"\x89PNG") else MP4Cover.FORMAT_JPEG
            audio.tags["covr"] = [MP4Cover(cover, imageformat=fmt)]
        audio.save()
    else:
        raise ValueError(f"Unsupported extension for retag: {ext}")


def _text(values: Any) -> list[str]:
    out = []
    for v in values or []:
        v = v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v)
        if v:
            out.append(v)
    return out


def read_tags(path: str) -> tuple[Tags, bool]:
    """(tags in spotify_tags' shape, has an embedded cover). Never raises:
    an unreadable file reads as untagged."""
    tags: Tags = {}
    has_cover = False
    try:
        audio: Any = MutagenFile(path)
        t = audio.tags if audio is not None else None
    except Exception as e:
        logging.debug(f"Could not read tags from {path}: {e}")
        return tags, False
    if t is None:
        return tags, bool(getattr(audio, "pictures", None))
    if hasattr(t, "getall"):  # ID3
        for key, frame in _ID3.items():
            frames = t.getall(frame.__name__)
            if frames:
                tags[key] = _text(frames[0].text)
        txxx = t.getall(f"TXXX:{_SPOTIFY_ID_TXXX}")
        if txxx:
            tags["spotify_track_id"] = _text(txxx[0].text)
        has_cover = bool(t.getall("APIC"))
    elif isinstance(audio, MP4):
        for key, atom in {**_MP4, **_MP4_FREEFORM}.items():
            if atom in t:
                tags[key] = _text(t[atom])
        if t.get("trkn"):
            n, total = t["trkn"][0]
            tags["track"] = [f"{n}/{total}" if total else str(n)]
        if t.get("disk"):
            tags["disc"] = [str(t["disk"][0][0])]
        has_cover = bool(t.get("covr"))
    else:  # Vorbis comments
        for key, name in _VORBIS.items():
            if t.get(name):
                tags[key] = _text(t[name])
        if t.get("tracknumber"):
            n, total = t["tracknumber"][0], (t.get("tracktotal") or [""])[0]
            tags["track"] = [f"{n}/{total}" if total and "/" not in n else n]
        has_cover = bool(getattr(audio, "pictures", None))
    return {k: v for k, v in tags.items() if v}, has_cover


def tag_diff(current: Tags, wanted: Tags) -> list[dict[str, Any]]:
    """Rows for the track page's preview: every key either side has."""
    rows = []
    for key in TAG_KEYS:
        if key not in current and key not in wanted:
            continue
        old, new = current.get(key, []), wanted.get(key)
        rows.append({
            "key": key, "label": TAG_LABELS[key], "current": "; ".join(old),
            "new": "; ".join(new) if new is not None else "; ".join(old),
            "changed": new is not None and new != old,
        })
    return rows


def tag_audio_file(file_path: str, tags: Tags, cover: bytes | None = None) -> bool:
    """Daemon-side tagging: never raises. WAV is skipped (counts as done),
    other unsupported formats and mutagen errors log and return False."""
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == '.wav':
            logging.warning("WAV tagging is not fully supported; skipping tags.")
        elif ext not in TAGGABLE_EXTS:
            logging.warning(f"Unsupported format for tagging: {ext}")
            return False
        else:
            write_tags(file_path, tags, cover)

        logging.info(f"Tagged {file_path} successfully.")
        return True

    except Exception as e:
        logging.error(f"Failed to tag {file_path}: {e}")
        return False
