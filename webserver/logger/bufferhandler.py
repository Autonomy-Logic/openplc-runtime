import logging
from collections import deque
from typing import List, Optional
import json
from datetime import datetime, timezone


class BufferHandler(logging.Handler):
    """
    Custom logging handler that stores log records in memory (FIFO).
    Logs are formatted using the attached formatter (JSON).
    """

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.buffer = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:
            self.handleError(record)

    def get_logs(self, count: Optional[int] = None) -> List[str]:
        """Retrieve logs from buffer."""
        print(f"Buffer: {list(self.buffer)}")
        if count is None or count > len(self.buffer):
            return list(self.buffer)
        return list(self.buffer)[-count:]

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

    # def normalize_buffer_logs(self, buffer_records):
    #     """
    #     Takes a list of log strings from buffer and returns a list of clean JSON dicts.
    #     """
    #     result = []
    #     json_extract = re.compile(r'(\{.*\})')  # match JSON inside log line

    #     for record in buffer_records:
    #         match = json_extract.search(record)
    #         if not match:
    #             continue

    #         try:
    #             raw_json = json.loads(match.group(1))
    #             # Convert unix timestamp → readable datetime
    #             ts = int(raw_json.get("timestamp", 0))
    #             dt = datetime.utcfromtimestamp(ts).isoformat() + "Z"

    #             entry = {
    #                 "timestamp": dt,
    #                 "level": raw_json.get("level", "INFO"),
    #                 "message": raw_json.get("message", "")
    #             }
    #             result.append(entry)
    #         except (json.JSONDecodeError, ValueError):
    #             continue

    #     return result

    def clear(self) -> None:
        self.buffer.clear()

    def __len__(self):
        return len(self.buffer)
