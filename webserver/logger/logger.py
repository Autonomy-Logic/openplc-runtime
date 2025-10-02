import logging
from .formatter import CustomFormatter
from .bufferhandler import BufferHandler


def get_logger(name: str = "logger", 
               level: int = logging.INFO, 
               use_buffer: bool = False) -> logging.Logger:
    """Return a logger instance with custom formatting."""

    collector_logger = logging.getLogger(name)
    collector_logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler()
    handler.setFormatter(CustomFormatter())
    collector_logger.addHandler(handler)

    if use_buffer:
        # Use buffer handler for log messages
        buffer_handler = BufferHandler()
        buffer_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        collector_logger.addHandler(buffer_handler)

    return collector_logger
