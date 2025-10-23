# tests/pytest/test_logger_buffer.py
import pytest

def test_buffer_stores_logs(test_logger):
    logger, buffer = test_logger
    logger.info("Hello World")
    logs = buffer.get_logs()
    assert len(logs) == 1
    assert "Hello World" in logs[0]["message"]

def test_buffer_clear_works(test_logger):
    logger, buffer = test_logger
    logger.info("A")
    logger.info("B")
    buffer.clear()
    logs = buffer.get_logs()
    assert len(logs) == 0

def test_multiple_logs_stored(test_logger):
    logger, buffer = test_logger
    messages = ["First log", "Second log", "Third log"]
    for msg in messages:
        logger.info(msg)
    logs = buffer.get_logs()
    assert len(logs) == 3
    for i, msg in enumerate(messages):
        assert msg in logs[i]["message"]

def test_log_levels_stored(test_logger):
    logger, buffer = test_logger
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")
    logs = buffer.get_logs()
    levels = [log["level"] for log in logs]
    assert levels == ["DEBUG", "INFO", "WARNING", "ERROR"]

def test_normalize_no_microseconds(test_logger):
    logger, buffer = test_logger
    normalized_ts = buffer.normalize_timestamp_no_microseconds("2024-01-01T12:00:00.123456+00:00")
    assert normalized_ts == "2024-01-01T12:00:00+0000"

def test_normalize_log_record(test_logger):
    logger, buffer = test_logger
    logger.info("Test log for normalization")
    logs = buffer.normalize_logs(buffer.get_logs())
    assert isinstance(logs, list)
    assert all("timestamp" in log for log in logs)

def test_filter_logs_by_level(test_logger):
    logger, buffer = test_logger
    logger.info("Info log")
    logger.error("Error log")
    logs = buffer.get_logs(level="ERROR")
    assert len(logs) == 1
    assert "Error log" in logs[0]["message"]

def test_filter_logs_by_min_id(test_logger):
    logger, buffer = test_logger
    buffer.clear()
    logger.info("Log 1")
    logger.info("Log 2")
    logger.info("Log 3")
    logs = buffer.get_logs(min_id=2)
    assert len(logs) == 2
    assert "Log 2" in logs[0]["message"]
    assert "Log 3" in logs[1]["message"]

