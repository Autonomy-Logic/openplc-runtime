# tests/conftest.py
import pytest
# from webserver.app import create_app

# @pytest.fixture
# def app():
#     """Fixture that provides a Flask app instance for testing."""
#     app = create_app()
#     app.config.update({
#         "TESTING": True,
#     })
#     return app

# @pytest.fixture
# def client(app):
#     """Fixture that provides a test client for the app."""
#     return app.test_client()

# @pytest.fixture
# def caplog_info_level(caplog):
#     """Fixture to automatically set logging to INFO during tests."""
#     with caplog.at_level("INFO"):
#         yield caplog
