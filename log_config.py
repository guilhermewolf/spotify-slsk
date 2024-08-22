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
    log_level = logging.INFO
    timezone = os.getenv('TZ', 'UTC')

    # Set up basic configuration
    logging.basicConfig(level=log_level)
    
    # Use the TimezoneFormatter
    formatter = TimezoneFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        tz=timezone
    )
    
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.getLogger().handlers = [handler]
    
    logging.info(f"Logging is configured with timezone: {timezone}")
