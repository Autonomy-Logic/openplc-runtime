import logging
import time
import json

class JsonFormatter(logging.Formatter):
    """Format log records as JSON strings."""

    def format(self, record: logging.LogRecord) -> str:
        log_dict = {
            "timestamp": str(int(record.created)),   # epoch seconds
            "level": record.levelname,
            "message": record.getMessage()
        }

        # Include optional fields if present
        if hasattr(record, "source"):
            log_dict["source"] = record.source

        return json.dumps(log_dict, ensure_ascii=False)
