"""Structured logging configuration with JSON formatter."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fusion.config import config


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id
        if hasattr(record, "duration_ms"):
            log_entry["duration_ms"] = record.duration_ms
        
        return json.dumps(log_entry)


class TextFormatter(logging.Formatter):
    """Human-readable text formatter for development."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as text."""
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        level = record.levelname
        name = record.name
        message = record.getMessage()
        
        log_line = f"[{timestamp}] {level:8s} {name}: {message}"
        
        if record.exc_info:
            log_line += "\n" + self.formatException(record.exc_info)
        
        return log_line


def setup_logging() -> None:
    """Configure application logging based on environment.
    
    Uses JSON format in production, human-readable format in development.
    Logs to stdout and optionally to file.
    """
    # Get log level from config
    log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # Remove existing handlers
    root_logger.handlers.clear()
    
    # Console handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    
    # Choose formatter based on config
    if config.LOG_FORMAT == "json":
        formatter = JSONFormatter()
    else:
        formatter = TextFormatter()
    
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # File handler (if log file is configured)
    if config.LOG_FILE and config.LOG_FILE != "/dev/null":
        try:
            # Create log directory if needed
            log_path = Path(config.LOG_FILE)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            
            file_handler = logging.FileHandler(config.LOG_FILE)
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except (OSError, PermissionError) as e:
            root_logger.warning(f"Failed to create file handler: {e}")
    
    # Set third-party loggers to WARNING to reduce noise
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("fastapi").setLevel(logging.INFO)
    
    root_logger.info(
        f"Logging configured: level={config.LOG_LEVEL}, "
        f"format={config.LOG_FORMAT}, env={config.ENV}"
    )
