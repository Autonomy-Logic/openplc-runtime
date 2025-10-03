import re
import logging
import time
import json
from typing import Optional, Dict

class LogParser:
    """Parse and re-log log entries from external sources as JSON."""

    LOG_PATTERN = re.compile(
        r'^\[(?P<timestamp>.*?)\] \[(?P<level>\w+)\] (?P<message>.*)$'
    )

    LEVEL_MAP = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }

    def __init__(self, collector_logger: logging.Logger):
        """
        Initialize parser with a logger instance to forward logs into.
        """
        self.collector_logger = collector_logger

    def parse_and_log(self, line: str) -> None:
        """
        Parse a line, then re-log it as JSON into the collector_logger.
        If not parsable, log as RAW JSON.
        """
        sline = line.strip()
        if not sline:
            return

        match = self.LOG_PATTERN.match(sline)
        if match:
            groups = match.groupdict()
            # If original timestamp can't be converted, fallback to now
            try:
                timestamp = str(int(time.mktime(time.strptime(groups["timestamp"], "%Y-%m-%dT%H:%M:%S"))))
            except Exception:
                timestamp = str(int(time.time()))

            log_dict = {
                "timestamp": timestamp,
                "level": groups["level"],
                "message": groups["message"]
            }

            level = self.LEVEL_MAP.get(groups["level"], logging.INFO)

        else:
            log_dict = {
                "timestamp": str(int(time.time())),
                "level": "INFO",
                "message": f"RAW: {sline}"
            }
            level = logging.INFO

        # Log as JSON string
        record = self.collector_logger.makeRecord(
            name="external",
            level=level,
            fn="",
            lno=0,
            msg=json.dumps(log_dict, ensure_ascii=False),
            args=(),
            exc_info=None
        )
        record.source = "external"
        self.collector_logger.handle(record)
