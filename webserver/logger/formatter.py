# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# logger/formatter.py
from datetime import datetime, timezone
import logging
import json
from . import config

class JsonFormatter(logging.Formatter):
    """Format log records as JSON strings."""

    def format(self, record):        
        msg = record.getMessage()

        # Try to detect pre-formatted JSON
        try:
            log_entry = json.loads(msg)
            log_entry["id"] = config.LoggerConfig.log_id
            # Already JSON — just make sure timestamp exists
            if "timestamp" not in log_entry:
                log_entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        
        except json.JSONDecodeError:
            # Not JSON, so create our standard JSON structure
            log_entry = {
                "id": config.LoggerConfig.log_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "message": msg,
            }
        
        return json.dumps(log_entry)


class HumanReadableFormatter(logging.Formatter):
    """Format log records as human-readable strings for stdout."""

    def format(self, record):
        msg = record.getMessage()

        # Try to detect pre-formatted JSON (from C runtime)
        try:
            log_entry = json.loads(msg)
            timestamp = log_entry.get("timestamp", "")
            level = log_entry.get("level", record.levelname)
            message = log_entry.get("message", msg)

            # Convert Unix timestamp to human-readable
            if timestamp and str(timestamp).isdigit():
                dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
                timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")
            elif timestamp:
                # Handle ISO 8601 format
                try:
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            else:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        except (json.JSONDecodeError, ValueError):
            # Not JSON - use record fields directly
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            level = record.levelname
            message = msg

        return f"[{timestamp}] [{level}] {message}"
