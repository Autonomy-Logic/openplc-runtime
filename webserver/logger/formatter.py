from datetime import datetime, timezone
import logging
import time
import json

class JsonFormatter(logging.Formatter):
    """Format log records as JSON strings."""
    def format(self, record):        
        msg = record.getMessage()

        # Try to detect pre-formatted JSON
        if msg.strip().startswith("{") and msg.strip().endswith("}"):
            try:
                parsed = json.loads(msg)
                # Already JSON — just make sure timestamp exists
                if "timestamp" not in parsed:
                    parsed["timestamp"] = datetime.now(timezone.utc).isoformat()
                return json.dumps(parsed)
            
            except json.JSONDecodeError:
                pass  # continue to default formatting

        # Not JSON, so create our standard JSON structure
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": msg,
        }
        return json.dumps(log_entry)

    # def format(self, record: logging.LogRecord) -> str:
    #     log_dict = {
    #         "timestamp": str(int(record.created)),   # epoch seconds
    #         "level": record.levelname,
    #         "message": record.getMessage()
    #     }

    #     # Include optional fields if present
    #     if hasattr(record, "source"):
    #         log_dict["source"] = record.source

    #     return json.dumps(log_dict, ensure_ascii=False)