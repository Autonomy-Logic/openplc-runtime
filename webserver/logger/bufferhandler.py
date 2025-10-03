import logging
from collections import deque
from typing import List, Optional

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
        if count is None or count > len(self.buffer):
            return list(self.buffer)
        return list(self.buffer)[-count:]

    def clear(self) -> None:
        self.buffer.clear()

    def __len__(self):
        return len(self.buffer)
