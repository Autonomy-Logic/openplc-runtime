import logging
from collections import deque
from typing import List, Optional
import json
from datetime import datetime, timezone
from threading import Lock


class BufferHandler(logging.Handler):
    """
    Custom logging handler that stores log records in memory (FIFO).
    Logs are formatted using the attached formatter (JSON).
    """
    _instance = None
    _lock = Lock()

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.buffer = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            try:
                self.buffer.append(self.format(record))
            except Exception:
                self.handleError(record)

    def get_logs(self, 
                 count: Optional[int] = None,
                 min_id: Optional[int] = None,
                 level: Optional[str] = None) -> List[str]:
        """Retrieve logs from buffer."""
        with self._lock:
        #     if count is None or count > len(self.buffer):
        #         return list(self.buffer)
        #     return list(self.buffer)[-count:]
        
        # with self._lock:
            filtered_logs = list(self.buffer)
            if min_id is not None:
                filtered_logs = [log for log in filtered_logs if log.get("id") >= min_id]
            if level is not None:
                filtered_logs = [log for log in filtered_logs if log.get("level") == level]
            print(f"Filtered logs: {filtered_logs}")
            return filtered_logs

    def normalize_logs(self, json_logs):
        normalized = []
        for entry in json_logs:
            try:
                data = json.loads(entry)

                # Normalize timestamp (convert unix timestamp → ISO 8601)
                ts = data.get("timestamp")

                # If it's numeric (e.g., 1759843183), convert it to ISO 8601 UTC
                if ts and ts.isdigit():
                    ts_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                    data["timestamp"] = ts_dt.isoformat()

                # Ensure minimal required fields
                data.setdefault("level", "INFO")
                data.setdefault("message", "")

                normalized.append(data)

            except (json.JSONDecodeError, TypeError) as e:
                # If something is not JSON, safely wrap it
                normalized.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": "ERROR",
                    "message": f"Malformed log: {entry}",
                })

        return normalized

    @classmethod
    def get_instance(cls):
        """Singleton accessor."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def clear(self) -> None:
        self.buffer.clear()

    def __len__(self):
        return len(self.buffer)
