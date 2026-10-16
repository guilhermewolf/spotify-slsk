"""Cover images from Spotify's CDN, fetched once and cached on the data volume.

Used for embedded album art (importer.tags) and the playlist folder's
cover (library.playlist_files). Every failure - network, timeout, a
redirect, an oversized or non-image body - is logged, remembered for a day
and returns None: artwork is never worth failing an import over.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from urllib.parse import urlsplit

import requests

TIMEOUT_SECONDS = 10
MAX_BYTES = 10 * 1024 * 1024  # Spotify's largest rendition is ~640px, < 300 KB
FAILURE_TTL_SECONDS = 24 * 3600  # a dead URL is retried once a day, not every cycle
CACHE_MAX_BYTES = 200 * 1024 * 1024  # least-recently-used images evicted beyond this
# Where Spotify serves album and playlist images: i.scdn.co, mosaic.scdn.co,
# *-images.scdn.co (daily mixes, This Is...), image-cdn-*.spotifycdn.com,
# pickasso/seed-mix-image/blend-playlist-covers.spotifycdn.com.
ALLOWED_HOST_SUFFIXES = (".scdn.co", ".spotifycdn.com")


def cache_dir() -> str:
    # Read at call time: tests point it at a sandbox (conftest).
    return os.getenv("ARTWORK_CACHE_DIR", "./data/artwork")


def is_image(data: bytes) -> bool:
    return data.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n"))


def allowed_url(url: str | None) -> bool:
    """https on a Spotify image CDN host only - the URL comes from the
    Spotify API, but this keeps the daemon from fetching anything else."""
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and host.endswith(ALLOWED_HOST_SUFFIXES)


def _evict(keep: str) -> None:
    """Drop the least recently used cached images beyond CACHE_MAX_BYTES."""
    try:
        entries = []
        with os.scandir(cache_dir()) as it:
            for e in it:
                if e.is_file() and e.name.endswith(".img"):
                    st = e.stat()
                    entries.append((st.st_mtime, st.st_size, e.path))
    except OSError:
        return
    total = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):
        if total <= CACHE_MAX_BYTES:
            break
        if path == keep:
            continue
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass


def fetch_image(url: str | None, key: str) -> bytes | None:
    """The image at `url`, from the cache under `key` when present. Only
    https URLs on Spotify's image CDNs are fetched, redirects are not
    followed, the body is capped at MAX_BYTES and must be a JPEG or PNG. A
    failure is remembered for FAILURE_TTL_SECONDS. Cache keys are reduced to
    [A-Za-z0-9_-] plus a hash of the URL, so a changed playlist cover is a
    new cache entry; a hit refreshes the entry's mtime (LRU eviction)."""
    if not allowed_url(url):
        if url:
            logging.warning(f"[artwork] Not fetching {url}: not a Spotify image URL")
        return None
    assert url is not None
    digest = hashlib.sha1(url.encode()).hexdigest()[:12]
    base = os.path.join(cache_dir(), f"{re.sub(r'[^A-Za-z0-9_-]', '_', key)}_{digest}")
    path, failed = f"{base}.img", f"{base}.fail"
    try:
        with open(path, "rb") as f:
            data = f.read()
        os.utime(path)
        return data
    except OSError:
        pass
    try:
        if time.time() - os.path.getmtime(failed) < FAILURE_TTL_SECONDS:
            return None
    except OSError:
        pass
    try:
        with requests.get(url, timeout=TIMEOUT_SECONDS, stream=True, allow_redirects=False) as resp:
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
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
        if os.path.exists(failed):
            os.remove(failed)
        _evict(keep=path)
        return data
    except Exception as e:
        logging.warning(f"[artwork] Could not fetch {url}: {e}; retrying after 24h")
        try:
            os.makedirs(cache_dir(), exist_ok=True)
            with open(failed, "w"):
                pass
        except OSError:
            pass
        return None


def album_cover(row: dict) -> bytes | None:
    """The track row's album cover (largest Spotify rendition)."""
    return fetch_image(row.get("cover_url"), f"album_{row.get('album_id') or 'unknown'}")
