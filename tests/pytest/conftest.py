# tests/conftest.py
import pytest
from webserver.logger import get_logger
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

logger, buffer = get_logger("test_logger", use_buffer=True)

@pytest.fixture(autouse=True)
def clean_logger_state():
    """Ensure buffer is cleared before each test."""
    buffer.clear()
    yield
    buffer.clear()

@pytest.fixture
def test_logger():
    return logger, buffer
