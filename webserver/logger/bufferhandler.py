# logger/bufferhandler.py
import logging
from collections import deque
from typing import List, Optional
import json
from datetime import datetime, timezone
from threading import Lock
from . import config


class BufferHandler(logging.Handler):
    """
    Custom logging handler that stores log records in memory (FIFO).
    Logs are formatted using the attached formatter (JSON).
    """
    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.buffer = deque(maxlen=capacity)
        self.records = []  # Store formatted log records as strings
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            try:
                record.log_id = config.LoggerConfig.next_log_id()
                formatted_record = self.format(record)
                self.records.append(formatted_record)
                self.buffer.append(formatted_record)
            except Exception:
                self.handleError(record)

    def filter_logs(self, logs, level=None, min_id=None, max_id=None):
        result = logs
        if level is not None:
            result = [log for log in result if log.get("level") == level]
        if min_id is not None:
            result = [log for log in result if log.get("id", 0) >= min_id]
        if max_id is not None:
            result = [log for log in result if log.get("id", 0) <= max_id]
        return result

    def get_logs(self, count: Optional[int] = None,
                 min_id: Optional[int] = None,
                 level: Optional[str] = None) -> List[str]:
        """Retrieve logs from buffer."""
        with self._lock:
            filtered_logs = [json.loads(item) for item in self.buffer]
            filtered_logs = self.filter_logs(filtered_logs, level=level, min_id=min_id)
            if count is not None and count < len(filtered_logs):
                filtered_logs = filtered_logs[-count:]
            return filtered_logs

    def normalize_timestamp_no_microseconds(self, ts: str) -> str:
        """Normalize ISO 8601 timestamp to remove microseconds."""
        dt = datetime.fromisoformat(ts)
        return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S%z")

    def normalize_logs(self, json_logs: List[dict]) -> List[dict]:
        """Normalize a list of log entries (dicts)."""
        normalized = []
        for data in json_logs:
            try:
                # Normalize timestamp (convert unix timestamp → ISO 8601)
                ts = data.get("timestamp")

                # If it's numeric (e.g., 1759843183), convert it to ISO 8601 UTC
                if ts and str(ts).isdigit():
                    ts_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                    data["timestamp"] = ts_dt.isoformat()

                # If it's ISO 8601 but has microseconds, strip them
                if "timestamp" in data:
                    data["timestamp"] = self.normalize_timestamp_no_microseconds(data["timestamp"])

                # Ensure minimal required fields
                data.setdefault("level", "INFO")
                data.setdefault("message", "")

                normalized.append(data)

            except (json.JSONDecodeError, TypeError, ValueError) as e:
                # If something is not JSON, safely wrap it
                normalized.append({
                    "id": None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": "ERROR",
                    "message": f"Malformed log: {data} ({e})",
                })

        return normalized

    def clear(self) -> None:
        self.buffer.clear()
        self.records.clear()
        config.LoggerConfig.reset_log_id()

    def __len__(self):
        return len(self.buffer)
