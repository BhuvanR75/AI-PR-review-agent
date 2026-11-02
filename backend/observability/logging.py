"""Logging setup. Structured enough to grep, plain enough to read in a terminal."""

from __future__ import annotations

import logging
import sys

from backend.settings import get_settings

_CONFIGURED = False


class _KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name:<34}"
        message = record.getMessage()
        extra = getattr(record, "context", None)
        if extra:
            rendered = " ".join(f"{k}={v}" for k, v in extra.items())
            message = f"{message} | {rendered}"
        line = f"{base} {message}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(level: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    resolved = level or ("INFO" if settings.environment != "test" else "WARNING")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_KeyValueFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(resolved)
    # These are chatty at INFO and say nothing we do not already log ourselves.
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")
    _CONFIGURED = True
