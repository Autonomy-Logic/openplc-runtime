import collections
import logging
from .formatter import CustomFormatter


log_buffer = collections.deque(maxlen=1000)

class BufferHandler(logging.Handler):
    def emit(self, record):
        # Use the formatter if it exists
        if self.formatter:
            timestamp = self.formatter.formatTime(record)
        else:
            # fallback: use record.created
            import datetime
            timestamp = datetime.datetime.fromtimestamp(record.created).isoformat()

        log_buffer.append({
            "time": timestamp,
            "level": record.levelname,
            "message": record.getMessage(),
            "source": getattr(record, "source", "python")
        })
        print(len(log_buffer))


def get_logger(name: str = "logger", level: int = logging.INFO) -> logging.Logger:
    """Return a logger instance with custom formatting."""
    collector_logger = logging.getLogger("collector")
    collector_logger.setLevel(logging.DEBUG)

    buffer_handler = BufferHandler()
    buffer_handler.setFormatter(logging.Formatter("%(asctime)s"))
    collector_logger.addHandler(buffer_handler)

    # Also print logs to console
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    collector_logger.addHandler(stream_handler)

    if not collector_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(CustomFormatter())
        collector_logger.addHandler(handler)

    return collector_logger
