"""The slskd HTTP client and the shared shutdown wiring.

The daemon thread shares one lazily-built ``SlskdClient`` (``get_client``);
anything running on another thread (the web UI's interactive search) builds
its own with ``new_client`` because the underlying requests.Session is not
thread-safe. Polling loops sleep through ``interruptible_sleep`` so SIGTERM
ends them promptly.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import slskd_api

_client = None
_shutdown_event = None


def set_shutdown_event(event: threading.Event | None) -> None:
    """Wire the caller's shutdown Event so our polling loops can exit promptly."""
    global _shutdown_event
    _shutdown_event = event


def interruptible_sleep(seconds: float) -> bool:
    """Return True if shutdown was requested during the sleep, False on timeout."""
    if _shutdown_event is None:
        time.sleep(seconds)
        return False
    return _shutdown_event.wait(seconds)


def new_client() -> slskd_api.SlskdClient:
    """Build a fresh SlskdClient from env."""
    return slskd_api.SlskdClient(
        host=os.getenv("SLSKD_HOST_URL", "http://slskd:5030"),
        api_key=os.getenv("SLSKD_API_KEY"),
        url_base=os.getenv("SLSKD_URL_BASE", ""),
    )


def get_client() -> slskd_api.SlskdClient:
    """Lazily build and cache the daemon's SlskdClient. Reads env at first call."""
    global _client
    if _client is None:
        _client = new_client()
    return _client


def shutting_down() -> bool:
    return _shutdown_event is not None and _shutdown_event.is_set()


def wait_for_slskd_healthy(
    host: str, api_key: str | None, timeout: float = 90, check_interval: float = 1
) -> bool:
    """Poll slskd until it reports connected+logged-in. Returns True on success.

    Returns False on timeout rather than raising: an unreachable slskd is an
    expected transient condition (compose start order, a restart upstream),
    not a reason to kill a daemon whose dashboard the operator needs most
    during exactly that outage. `block_until_slskd_healthy` owns the retrying.
    """
    logging.info(f"Waiting for slskd at {host} (timeout: {timeout}s)...")
    client = get_client()

    start = time.time()
    last_err = None
    while time.time() - start < timeout:
        if shutting_down():
            return False
        try:
            state = client.application.state()
            if state['server'].get('isConnected') and state['server'].get('isLoggedIn'):
                logging.info("slskd is healthy and connected.")
                return True
        except Exception as e:
            last_err = e

        if int(time.time() - start) % 5 == 0:
            logging.debug(f"Still waiting for slskd... (last error: {last_err})")

        if interruptible_sleep(check_interval):
            return False

    logging.warning(f"slskd did not become healthy within {timeout}s: {last_err}")
    return False
