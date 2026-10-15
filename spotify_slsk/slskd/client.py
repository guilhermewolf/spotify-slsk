"""The slskd HTTP client and the shared shutdown wiring.

The daemon thread shares one lazily-built ``SlskdClient`` (``get_client``);
anything running on another thread (the web UI's interactive search) builds
its own with ``new_client`` because the underlying requests.Session is not
thread-safe. Polling loops sleep through ``interruptible_sleep`` so SIGTERM
ends them promptly.
"""
import os
import time

import slskd_api

_client = None
_shutdown_event = None


def set_shutdown_event(event) -> None:
    """Wire the caller's shutdown Event so our polling loops can exit promptly."""
    global _shutdown_event
    _shutdown_event = event


def interruptible_sleep(seconds: float) -> bool:
    """Return True if shutdown was requested during the sleep, False on timeout."""
    if _shutdown_event is None:
        time.sleep(seconds)
        return False
    return _shutdown_event.wait(seconds)


def new_client():
    """Build a fresh SlskdClient from env."""
    return slskd_api.SlskdClient(
        host=os.getenv("SLSKD_HOST_URL", "http://slskd:5030"),
        api_key=os.getenv("SLSKD_API_KEY"),
        url_base=os.getenv("SLSKD_URL_BASE", ""),
    )


def get_client():
    """Lazily build and cache the daemon's SlskdClient. Reads env at first call."""
    global _client
    if _client is None:
        _client = new_client()
    return _client


def shutting_down() -> bool:
    return _shutdown_event is not None and _shutdown_event.is_set()
