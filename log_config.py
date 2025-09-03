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

def setup_logging():
    """
    Configure root logging:
      - Level from $LOGLEVEL (default INFO)
      - Timestamps rendered in $TIMEZONE (default UTC)
      - Single StreamHandler with our TimezoneFormatter
    """
    log_level_str = os.getenv("LOGLEVEL", "INFO").upper()
    timezone = os.getenv("TIMEZONE", "UTC")

    # Map to logging level, default to INFO if unknown
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Ensure basicConfig has the desired level
    logging.basicConfig(level=log_level)

    # Build our timezone-aware formatter
    formatter = TimezoneFormatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        tz=timezone,
    )

    # Replace existing handlers with a single stream handler using our formatter
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)

    logging.info(f"Logging is configured with timezone: {timezone}")
    logging.info(f"Logging level is set to: {log_level_str}")
