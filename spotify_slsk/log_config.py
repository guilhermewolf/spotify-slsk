import logging
import os
import pytz
from datetime import datetime

class TimezoneFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, tz=None):
        super().__init__(fmt, datefmt)
        self.tz = pytz.timezone(tz) if tz else pytz.utc

    def formatTime(self, record, datefmt=None):
        record_time = datetime.fromtimestamp(record.created, self.tz)
        return record_time.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

_NOISY_LIBRARIES = (
    "urllib3",
    "urllib3.connectionpool",
    "requests",
    "spotipy",
    "spotipy.client",
    "spotipy.cache_handler",
    "spotipy.oauth2",
)


def setup_logging():
    """
    Configure root logging:
      - App-code level from $LOGLEVEL (default INFO).
      - Third-party library level from $LIB_LOGLEVEL (default WARNING) — so
        LOGLEVEL=DEBUG doesn't drown us in urllib3/spotipy traffic.
      - Timestamps rendered in $TIMEZONE (default UTC).
    """
    log_level_str = os.getenv("LOGLEVEL", "INFO").upper()
    lib_log_level_str = os.getenv("LIB_LOGLEVEL", "WARNING").upper()
    timezone = os.getenv("TIMEZONE", "UTC")

    log_level = getattr(logging, log_level_str, logging.INFO)
    lib_log_level = getattr(logging, lib_log_level_str, logging.WARNING)

    formatter = TimezoneFormatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        tz=timezone,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    # Mirror every record into the in-memory ring buffer so the web UI can
    # tail it. Stays alongside the stdout handler — Docker still captures the
    # stream.
    from .runtime_state import RingBufferHandler
    buffer_handler = RingBufferHandler()
    buffer_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler, buffer_handler]
    root.setLevel(log_level)

    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(lib_log_level)

    logging.info(
        f"Logging configured: app={log_level_str}, libs={lib_log_level_str}, tz={timezone}"
    )
