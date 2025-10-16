from datetime import datetime, timezone
import logging
import json


class JsonFormatter(logging.Formatter):
    """Format log records as JSON strings."""
    log_id = 0

    def format(self, record):        
        msg = record.getMessage()
        self.log_id += 1

        # Try to detect pre-formatted JSON
        if msg.strip().startswith("{") and msg.strip().endswith("}"):
            try:
                parsed = json.loads(msg)
                # Already JSON — just make sure timestamp exists
                if "timestamp" not in parsed:
                    parsed["timestamp"] = datetime.now(timezone.utc).isoformat()
                parsed["id"] = self.log_id
                return json.dumps(parsed)
            
            except json.JSONDecodeError:
                pass  # continue to default formatting

        # Not JSON, so create our standard JSON structure
        log_entry = {
            "id": self.log_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": msg,
        }
        return json.dumps(log_entry)

