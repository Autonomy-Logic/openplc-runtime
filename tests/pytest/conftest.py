# tests/conftest.py
import pytest
from webserver.logger import get_logger
import sys
import os

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
