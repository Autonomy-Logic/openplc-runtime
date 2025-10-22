# logger/logger.py
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

    # Always ensure a StreamHandler exists
    if not any(isinstance(h, logging.StreamHandler) for h in collector_logger.handlers):
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(JsonFormatter())
        collector_logger.addHandler(stream_handler)

    buffer_handler = None
    if use_buffer and not any(isinstance(h, BufferHandler) for h in collector_logger.handlers):
        buffer_handler = BufferHandler()
        buffer_handler.setFormatter(JsonFormatter())
        collector_logger.addHandler(buffer_handler)
    
    return collector_logger, buffer_handler
