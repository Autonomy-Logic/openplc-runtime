# tests/test_logging.py

def test_ping_logs_message(client, caplog_info_level):
    """Check if /ping route logs a message at INFO level."""
    response = client.get("/ping")

    assert response.status_code == 200
    assert any(
        "Ping endpoint was called" in message
        for message in caplog_info_level.messages
    )
