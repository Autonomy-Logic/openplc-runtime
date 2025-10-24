# Python Logging Module Documentation

## Overview

This logging module provides a structured, JSON-based logging system with in-memory buffering. It is designed for runtime environments where log data needs to be serialized, transmitted, or parsed consistently. The module integrates seamlessly with the Python `logging` standard library but extends it with JSON formatting, a shared buffer, and parsing utilities.

---

## Module Structure

```
logger/
│
├── __init__.py          # Main entry point, global configuration
├── logger.py            # Factory for logger instances
├── config.py            # configuration variables and thread safety lock
├── formatter.py         # JSON formatter for structured logs
├── bufferhandler.py     # In-memory log buffer (not shown above)
└── parser.py            # Parses and normalizes incoming log lines
```

---

## Components

### 1. `get_logger()` (from `__init__.py` and `logger.py`)

#### Purpose

Creates and returns a configured logger instance. The logger outputs JSON-formatted messages and can optionally store logs in a shared buffer.

#### Signature

```python
get_logger(name="runtime", use_buffer: bool = False)
```

#### Behavior

- Sets log level to `DEBUG`.
- Ensures a `StreamHandler` is attached (outputs to `stdout`).
- Optionally adds a shared `BufferHandler` for in-memory collection.
- Uses `JsonFormatter` for consistent output.

#### Returns

```python
(logger_instance, buffer_handler)
```

#### Example

```python
from logger import get_logger

logger, buffer = get_logger("logger", use_buffer=True)
logger.info("System initialized")
```

---

### 2. `JsonFormatter`

#### Location

`logger/formatter.py`

#### Purpose

Formats log records as structured JSON objects, ensuring every log entry contains `id`, `timestamp`, `level`, and `message` fields.

#### Key Features

- Automatically adds timestamps in ISO 8601 UTC format.
- Detects pre-formatted JSON and augments it with missing fields.
- Includes a global `log_id` for log correlation.

#### Example Output

```json
{
  "id": "1",
  "timestamp": "2025-10-22T19:03:04.123456Z",
  "level": "INFO",
  "message": "System initialized"
}
```

---

### 3. `BufferHandler`

#### Location

`logger/bufferhandler.py`

#### Purpose

Stores log records in memory for later retrieval or transmission. Useful for APIs that expose logs or batch log uploads.

#### Notes

- Shared across all logger instances.
- Can be flushed or accessed by components that need recent log history.

---

### 4. `LogParser`

#### Location

`logger/parser.py`

#### Purpose

Normalizes incoming log lines into structured JSON and re-logs them via the Python logging system.

#### Key Features

- Detects JSON logs and preserves their structure.
- Supports pattern `[LEVEL] message` using regex.
- Defaults to INFO level if no match is found.
- Converts parsed logs to JSON and forwards to the configured logger.

#### Example Usage

```python
from logger import LogParser, get_logger

logger, _ = get_logger("logger")
parser = LogParser(logger)

parser.parse_and_log("[ERROR] Connection failed")
parser.parse_and_log('{"level":"INFO","message":"Reconnected"}')
```

#### Example Output

```json
{
  "timestamp": "1729587834",
  "level": "ERROR",
  "message": "Connection failed"
}
```

---

## Thread Safety

This logging architecture is designed to be **thread-safe**, following Python’s built-in `logging` guarantees:

- The Python `logging` module ensures that handler operations are protected by internal locks (`threading.RLock`).
- Each handler (e.g., `StreamHandler`, `BufferHandler`) can safely receive log records from multiple threads simultaneously.
- The shared `BufferHandler` ensures consistent appends by relying on the thread-safe `logging.Handler.emit()` method.
- For applications with heavy concurrency (e.g., multithreaded servers), this architecture prevents race conditions during log writes or JSON serialization.

**Recommendations for Multithreaded Use:**

- Avoid modifying shared handler state (like buffer lists) outside `emit()` or synchronized contexts.
- Use `QueueHandler` or `QueueListener` for extremely high-throughput scenarios.
- Always use a dedicated logger instance per module, not per thread.

---

## Example: Combined Usage

```python
from logger import get_logger, LogParser

# Initialize logger
logger, buffer = get_logger("runtime", use_buffer=True)

# Log directly
logger.info("Startup complete")

# Parse and re-log an external message
parser = LogParser(logger)
parser.parse_and_log("[WARNING] Low voltage detected")

# Access buffered logs
for record in buffer.buffer:
    print(record)
```

**Sample Output:**

```json
{"id": "runtime-01", "timestamp": "2025-10-22T19:12:44.312Z", "level": "INFO", "message": "Startup complete"}
{"id": "runtime-01", "timestamp": "2025-10-22T19:12:44.325Z", "level": "WARNING", "message": "Low voltage detected"}
```

---

## 🧪 Development & Testing

This section guides developers on how to **test and validate the logging module** during development.

---

### 1. Unit Testing

The module can be tested using Python’s built-in `unittest` framework or `pytest`.

**Example: `test_logger.py`**

```python
import unittest
from logger import get_logger, LogParser

class TestLogger(unittest.TestCase):
    def setUp(self):
        self.logger, self.buffer = get_logger("test_logger", use_buffer=True)
        self.parser = LogParser(self.logger)

    def test_info_log(self):
        self.logger.info("Test info message")
        self.assertIn("Test info message", self.buffer.buffer[-1])

    def test_warning_log(self):
        self.logger.warning("Test warning")
        log_entry = self.buffer.buffer[-1]
        self.assertIn('"level": "WARNING"', log_entry)

    def test_parse_plain_text(self):
        self.parser.parse_and_log("[ERROR] External error")
        log_entry = self.buffer.buffer[-1]
        self.assertIn('"level": "ERROR"', log_entry)
        self.assertIn("External error", log_entry)

    def test_parse_json_log(self):
        json_line = '{"level": "INFO", "message": "External JSON"}'
        self.parser.parse_and_log(json_line)
        log_entry = self.buffer.buffer[-1]
        self.assertIn("External JSON", log_entry)

if __name__ == "__main__":
    unittest.main()
```

Run tests:

```bash
python -m unittest test_logger.py
# or with pytest
pytest test_logger.py
```

---

### 2. Buffer Validation

Check that logs are properly stored in the buffer and old entries are trimmed if using a capacity limit.

```python
logger, buffer = get_logger("runtime", use_buffer=True)

for i in range(1100):  # assuming default capacity 1000
    logger.info(f"Message {i}")

assert len(buffer.buffer) <= 1000
```

---

### 3. Parser Validation

Ensure the `LogParser` correctly handles:

1. JSON input
2. Regex pattern `[LEVEL] message`
3. Plain-text messages

**Test Example:**

```python
parser.parse_and_log("[INFO] Hello World")
parser.parse_and_log('{"level":"DEBUG","message":"Debug JSON"}')
parser.parse_and_log("Just a string message")
```

Check that all entries are JSON-formatted in the buffer.

---

### 4. Thread-Safety Testing

Simulate concurrent logging from multiple threads:

```python
import threading

def log_messages(logger, count=100):
    for i in range(count):
        logger.info(f"Thread log {i}")

logger, buffer = get_logger("thread_test", use_buffer=True)

threads = [threading.Thread(target=log_messages, args=(logger,)) for _ in range(5)]
[t.start() for t in threads]
[t.join() for t in threads]

print(f"Total logs in buffer: {len(buffer.buffer)}")
```

All entries should appear in the buffer without corruption or race conditions.

---

### 5. Coverage

Measure test coverage:

```bash
pytest --cov=logger test_logger.py
```

This ensures all log paths, parser cases, and formatter scenarios are exercised.
