import logging
import logging.config
from .logger import get_logger
from .parser import LogParser
from .bufferhandler import BufferHandler
from .formatter import JsonFormatter

__all__ = ["get_logger", "LogParser", "BufferHandler", "JsonFormatter"]
__version__ = "0.1"
__author__ = "Autonomy"
__license__ = "MIT"
__description__ = "RestAPI interface for runtime core"


# Configure logging once
# logging.config.dictConfig(
#     {
#         "version": 1,
#         "formatters": {
#             "default": {
#                 "format": "[%(levelname)s] %(asctime)s - %(name)s - %(message)s",
#                 "datefmt": "%H:%M:%S",
#             }
#         },
#         "handlers": {
#             "console": {
#                 "class": "logging.StreamHandler",
#                 "formatter": "default",
#                 "level": "DEBUG",
#             }
#         },
#         "root": {"level": "DEBUG", "handlers": ["console"]},
#     }
# )

# Single global buffer for all logs
shared_buffer_handler = BufferHandler()

formatter = JsonFormatter()
shared_buffer_handler.setFormatter(formatter)

def get_logger(name="runtime", use_buffer: bool = False):
    """Return a logger that shares the same buffer handler."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if use_buffer:
        if not any(isinstance(h, BufferHandler) for h in logger.handlers):
            logger.addHandler(shared_buffer_handler)

    return logger, shared_buffer_handler
