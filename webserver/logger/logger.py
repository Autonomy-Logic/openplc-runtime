import logging
from .formatter import JsonFormatter
from .bufferhandler import BufferHandler


def get_logger(name: str = "logger", 
               level: int = logging.INFO, 
               use_buffer: bool = False):
    """Return a logger instance with custom formatting."""

    collector_logger = logging.getLogger(name)
    collector_logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    collector_logger.addHandler(handler)

    buffer_handler = None

    if use_buffer:
        # Use buffer handler for log messages
        buffer_handler = BufferHandler()
        buffer_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        collector_logger.addHandler(buffer_handler)
    
    if use_buffer:
        # Find buffer handler again if it already exists
        if buffer_handler is None:
            for h in collector_logger.handlers:
                if isinstance(h, BufferHandler):
                    buffer_handler = h
                    break
        return collector_logger, buffer_handler
    else:
        return collector_logger, None

    # return collector_logger
