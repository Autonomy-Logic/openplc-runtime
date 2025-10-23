# logger/config.py
import threading
import logging

class LoggerConfig:
    log_id: int = 0
    log_level: int = logging.INFO
    use_buffer: bool = False
    _log_id_lock = threading.Lock()

    @classmethod
    def next_log_id(cls) -> int:
        """Thread-safe increment and return of global log ID."""
        with cls._log_id_lock:
            cls.log_id += 1
            return cls.log_id
    
    @classmethod
    def reset_log_id(cls) -> None:
        """Reset the global log ID to zero."""
        with cls._log_id_lock:
            cls.log_id = 0
