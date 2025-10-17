import logging
import pytest

from webserver.logger import get_logger, BufferHandler

def test_logger_creates_handlers():
    # Reset previous handlers
    logger, _ = get_logger("test_logger", use_buffer=True)
    logger.handlers.clear()
    logger, _ = get_logger("test_logger", use_buffer=True)

    # Assert logger level
    assert logger.level == logging.DEBUG

    # It should have at least 2 handlers (stream + buffer)
    handler_types = [type(h) for h in logger.handlers]
    assert logging.StreamHandler in handler_types
    assert BufferHandler in handler_types

def test_buffer_handler_captures_logs():
    logger, _ = get_logger("buffer_logger", use_buffer=True)

    # Get the buffer handler
    buffer_handler = next(h for h in logger.handlers if isinstance(h, BufferHandler))

    # Log something
    msg = "hello pytest logging"
    logger.info(msg)

    # The buffer should have stored the formatted message
    assert any(msg in record for record in buffer_handler.records)

def test_logger_does_not_duplicate_handlers():
    # Calling get_logger twice should not create duplicate handlers
    logger1, _ = get_logger("same_logger", use_buffer=True)
    logger2, _ = get_logger("same_logger", use_buffer=True)

    assert logger1 is logger2  # same logger object
    assert len(logger1.handlers) == len(logger2.handlers)  # still only 2 handlers
