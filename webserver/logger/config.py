# logger/config.py
from dataclasses import dataclass
import logging

@dataclass
class LoggerConfig:
    log_id: int = 0
    log_level: int = logging.INFO
    use_buffer: bool = False