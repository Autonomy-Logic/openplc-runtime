import logging
import logging.config
from .logger import get_logger
from .parser import LogParser
from .bufferhandler import BufferHandler

__all__ = ["get_logger", "LogParser", "BufferHandler"]
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
