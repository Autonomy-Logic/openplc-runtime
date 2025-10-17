# tests/pytest/test_logger_buffer.py

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