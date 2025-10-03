# logger/parser.py
import logging
import re
import time
import json

LOG_PATTERN = re.compile(r'^\[(?P<level>\w+)\]\s*(?P<message>.*)$')

LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class LogParser:
    def __init__(self, collector_logger: logging.Logger):
        self.collector_logger = collector_logger

    def parse_and_log(self, line: str):
        """Parse incoming log line and re-log it in normalized JSON format."""
        sline = line.strip()
        if not sline:
            return

        timestamp = int(time.time())
        level_name = "INFO"
        level = logging.INFO
        message = sline

        # Case 1: JSON log already
        try:
            parsed = json.loads(sline)
            if isinstance(parsed, dict) and "message" in parsed:
                # Preserve incoming JSON fields, but ensure timestamp is present
                parsed.setdefault("timestamp", str(timestamp))
                level_name = parsed.get("level", "INFO")
                level = LEVEL_MAP.get(level_name, logging.INFO)
                log_entry = parsed
            else:
                raise ValueError("Not a valid log JSON dict")
        except (json.JSONDecodeError, ValueError):
            # Case 2: Regex log like "[INFO] Something"
            match = LOG_PATTERN.match(sline)
            if match:
                level_name = match["level"]
                level = LEVEL_MAP.get(level_name, logging.INFO)
                message = match["message"]
            else:
                message = sline

            log_entry = {
                "timestamp": str(timestamp),
                "level": level_name,
                "message": message
            }

        # Create final JSON string
        json_log = json.dumps(log_entry, ensure_ascii=False)

        # Push into Python logging
        record = self.collector_logger.makeRecord(
            name="external",
            level=level,
            fn="",
            lno=0,
            msg=json_log,
            args=(),
            exc_info=None
        )
        record.source = "external"
        self.collector_logger.handle(record)
