import logging
import logging.config
import collections

__version__ = "0.1"
__author__ = "Autonomy"
__license__ = "MIT"
__description__ = "RestAPI interface for runtime core"


# Configure logging once
logging.config.dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {
                "format": "[%(levelname)s] %(asctime)s - %(name)s - %(message)s",
                "datefmt": "%H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": "DEBUG",
            }
        },
        "root": {"level": "DEBUG", "handlers": ["console"]},
    }
)

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

collector_logger = logging.getLogger("collector")
collector_logger.setLevel(logging.DEBUG)

buffer_handler = BufferHandler()
buffer_handler.setFormatter(logging.Formatter("%(asctime)s"))
collector_logger.addHandler(buffer_handler)

# Also print logs to console
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
collector_logger.addHandler(stream_handler)
