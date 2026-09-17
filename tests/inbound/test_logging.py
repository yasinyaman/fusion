"""Tests for observability.logging."""

import io
import json
import logging

from fusion.application.settings import Settings
from fusion.observability.logging import JSONFormatter, TextFormatter, setup_logging


def _record(msg="hello", level=logging.INFO):
    return logging.LogRecord("fusion.test", level, __file__, 1, msg, None, None)


def test_json_formatter_shape():
    entry = json.loads(JSONFormatter().format(_record()))
    assert entry["level"] == "INFO"
    assert entry["logger"] == "fusion.test"
    assert entry["message"] == "hello"
    assert entry["timestamp"].endswith("+00:00")


def test_text_formatter():
    line = TextFormatter().format(_record("hi", logging.WARNING))
    assert "WARNING" in line and "fusion.test: hi" in line


def test_setup_logging_to_stream(tmp_path):
    stream = io.StringIO()
    settings = Settings(log_level="DEBUG", log_format="json", log_file=str(tmp_path / "f.log"))
    setup_logging(settings, stream=stream)
    logging.getLogger("fusion.probe").debug("probe-message")
    assert "probe-message" in stream.getvalue()
    assert "probe-message" in (tmp_path / "f.log").read_text()
    logging.getLogger().handlers.clear()


def test_setup_logging_bad_file_is_warning_only(tmp_path):
    stream = io.StringIO()
    settings = Settings(log_format="text", log_file=str(tmp_path / "f.log" / "impossible"))
    (tmp_path / "f.log").write_text("not a dir")
    setup_logging(settings, stream=stream)
    assert "Failed to create log file handler" in stream.getvalue()
    logging.getLogger().handlers.clear()
