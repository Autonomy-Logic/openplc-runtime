# tests/pytest/test_logger_init.py
def test_logger_initializes_correctly(test_logger):
    logger, buffer = test_logger
    assert logger.name == "test_logger"
    assert logger.level == 10  # logging.DEBUG
    assert len(logger.handlers) >= 1
    assert isinstance(buffer.get_logs(), list)
