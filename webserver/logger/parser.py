import logging
import re
from typing import Optional, Dict

LOG_PATTERN = re.compile(r"""
    ^\[(?P<time>[\d-]+\s+[\d:]+)\]   # timestamp inside [ ]
    \s+\[(?P<level>[A-Z]+)\]         # level inside [ ]
    \s+(?P<message>.*)$              # the rest is message
""", re.VERBOSE)

LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

class LogParser:
    """Parse log entries from raw strings into structured dicts."""

    LOG_PATTERN = re.compile(
        r'^\[(?P<timestamp>.*?)\] \[(?P<level>\w+)\] (?P<message>.*)$'
    )

    @classmethod
    def parse(cls, raw_log: str) -> Optional[Dict[str, str]]:
        match = cls.LOG_PATTERN.match(raw_log.strip())
        if not match:
            return None
        return match.groupdict()
