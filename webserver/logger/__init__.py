# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# logger/__init__.py
import logging
import sys

from .parser import LogParser
from .bufferhandler import BufferHandler
from .formatter import JsonFormatter, HumanReadableFormatter
from .config import LoggerConfig

__all__ = ["get_logger", "LogParser", "BufferHandler", "JsonFormatter", "HumanReadableFormatter"]
__version__ = "0.1"
__author__ = "Autonomy"
__license__ = "MIT"
__description__ = "RestAPI interface for runtime core"

# Single global buffer for all logs
shared_buffer_handler = BufferHandler()

formatter = JsonFormatter()
shared_buffer_handler.setFormatter(formatter)


def _get_effective_level():
    """Return the effective log level based on print_debug config."""
    return logging.DEBUG if LoggerConfig.print_debug else logging.INFO


def get_logger(name="runtime", use_buffer: bool = False):
    """Return a logger that shares the same buffer handler."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    effective_level = _get_effective_level()

    # Ensure a StreamHandler exists
    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, BufferHandler)
        for h in logger.handlers
    ):
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(HumanReadableFormatter())
        logger.addHandler(stream_handler)

    # Ensure BufferHandler exists if requested
    if use_buffer and not any(isinstance(h, BufferHandler) for h in logger.handlers):
        logger.addHandler(shared_buffer_handler)

    # Always update all handler levels to reflect current config
    for h in logger.handlers:
        h.setLevel(effective_level)

    return logger, shared_buffer_handler
