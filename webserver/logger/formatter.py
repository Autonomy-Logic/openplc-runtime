import logging
import datetime


class CustomFormatter(logging.Formatter):
    """Custom formatter with timestamp, level, and message."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.datetime.fromtimestamp(record.created).isoformat()
        return f"[{timestamp}] [{record.levelname}] {record.getMessage()}"
