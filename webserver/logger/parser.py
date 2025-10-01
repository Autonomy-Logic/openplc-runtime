import re
import logging
from typing import Optional, Dict

class LogParser:
    """Parse and re-log log entries from external sources."""

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

    @classmethod
    def parse(cls, raw_log: str) -> Optional[Dict[str, str]]:
        """Parse raw log string into dict, or None if it doesn't match."""
        match = cls.LOG_PATTERN.match(raw_log.strip())
        if not match:
            return None
        return match.groupdict()

    def parse_and_log(self, line: str) -> None:
        """
        Parse a line, then re-log it into the collector_logger.
        If not parsable, re-log as RAW.
        """
        sline = line.strip()
        if not sline:
            return

        match = self.LOG_PATTERN.match(sline)
        if match:
            groups = match.groupdict()
            level = self.LEVEL_MAP.get(groups["level"], logging.INFO)
            message = groups["message"]

            record = self.collector_logger.makeRecord(
                name="external",
                level=level,
                fn="",
                lno=0,
                msg=message,
                args=(),
                exc_info=None
            )
            record.source = "external"
            self.collector_logger.handle(record)
        else:
            record = self.collector_logger.makeRecord(
                name="external",
                level=logging.INFO,
                fn="",
                lno=0,
                msg=f"RAW: {sline}",
                args=(),
                exc_info=None
            )
            record.source = "external"
            self.collector_logger.handle(record)
