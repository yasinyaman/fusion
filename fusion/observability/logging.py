"""Structured logging configuration (JSON in production, text in development).

Nothing runs at import time; call ``setup_logging(settings)`` from an entry
point.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from fusion.application.settings import Settings


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        for extra in ("request_id", "duration_ms"):
            if hasattr(record, extra):
                entry[extra] = getattr(record, extra)
        return json.dumps(entry)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {record.levelname:8s} {record.name}: {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(settings: Settings, stream: TextIO | None = None) -> None:
    """Configure the root logger from settings.

    ``stream`` defaults to stdout; the MCP server passes stderr because
    stdout carries the protocol.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    formatter: logging.Formatter = (
        JSONFormatter() if settings.log_format == "json" else TextFormatter()
    )
    console = logging.StreamHandler(stream or sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    if settings.log_file and settings.log_file != "/dev/null":
        try:
            Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(settings.log_file)
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as e:
            root.warning("Failed to create log file handler: %s", e)

    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for server in ("uvicorn", "fastapi"):
        logging.getLogger(server).setLevel(logging.INFO)

    root.info(
        "Logging configured: level=%s, format=%s, env=%s",
        settings.log_level,
        settings.log_format,
        settings.env,
    )
