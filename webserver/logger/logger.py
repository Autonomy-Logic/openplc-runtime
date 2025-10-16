import logging
import sys
from .formatter import JsonFormatter
from .bufferhandler import BufferHandler


def get_logger(name: str = "logger", 
               level: int = logging.INFO, 
               use_buffer: bool = False):
    """Return a logger instance with custom formatting."""

    collector_logger = logging.getLogger(name)
    collector_logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    collector_logger.addHandler(handler)

    buffer_handler = None
    # Use buffer handler for log messages
    if use_buffer:
        buffer_handler = BufferHandler()
        buffer_handler.setFormatter(JsonFormatter())
        collector_logger.addHandler(buffer_handler)
    
    return collector_logger, buffer_handler
