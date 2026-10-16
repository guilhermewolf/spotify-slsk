"""Cover images from Spotify's CDN, fetched once and cached on the data volume.

Used for embedded album art (importer.tags) and the playlist folder's
cover.jpg (library.playlist_files). Every failure - network, timeout, an
oversized or non-image body - is logged and returns None: artwork is never
worth failing an import over.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re

import requests

TIMEOUT_SECONDS = 10
MAX_BYTES = 10 * 1024 * 1024  # Spotify's largest rendition is ~640px, < 300 KB


def cache_dir() -> str:
    # Read at call time: tests point it at a sandbox (conftest).
    return os.getenv("ARTWORK_CACHE_DIR", "./data/artwork")


def is_image(data: bytes) -> bool:
    return data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"))


def fetch_image(url: str | None, key: str) -> bytes | None:
    """The image at `url`, from the cache under `key` when present. Only
    https URLs are fetched; the body is capped at MAX_BYTES and must be a
    JPEG or PNG. Cache keys are reduced to [A-Za-z0-9_-] plus a hash of the
    URL, so a changed playlist cover is a new cache entry."""
    if not url or not url.startswith("https://"):
        return None
    digest = hashlib.sha1(url.encode()).hexdigest()[:12]
    path = os.path.join(cache_dir(), f"{re.sub(r'[^A-Za-z0-9_-]', '_', key)}_{digest}.img")
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        pass
    try:
        with requests.get(url, timeout=TIMEOUT_SECONDS, stream=True) as resp:
            resp.raise_for_status()
            data = b""
            for chunk in resp.iter_content(64 * 1024):
                data += chunk
                if len(data) > MAX_BYTES:
                    raise ValueError(f"larger than {MAX_BYTES} bytes")
        if not is_image(data):
            raise ValueError("not a JPEG or PNG")
        os.makedirs(cache_dir(), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return data
    except Exception as e:
        logging.warning(f"[artwork] Could not fetch {url}: {e}")
        return None


def album_cover(row: dict) -> bytes | None:
    """The track row's album cover (largest Spotify rendition)."""
    return fetch_image(row.get("cover_url"), f"album_{row.get('album_id') or 'unknown'}")
