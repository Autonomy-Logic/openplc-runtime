# logger/formatter.py
from datetime import datetime, timezone
import logging
import json
from . import config

class JsonFormatter(logging.Formatter):
    """Format log records as JSON strings."""

    def format(self, record):        
        msg = record.getMessage()

        # Try to detect pre-formatted JSON
        try:
            log_entry = json.loads(msg)
            log_entry["id"] = config.LoggerConfig.log_id
            # Already JSON — just make sure timestamp exists
            if "timestamp" not in log_entry:
                log_entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        except json.JSONDecodeError:
            # Not JSON, so create our standard JSON structure
            log_entry = {
                "id": config.LoggerConfig.log_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "message": msg,
            }
        
        return json.dumps(log_entry)

