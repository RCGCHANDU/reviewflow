"""Structured logging.

Log lines carry workflow/stage/role metadata and never carry document content or
secrets: :func:`redact` is applied to anything free-text that could contain them.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

_LOGGER_NAME = "reviewflow"
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
]


def redact(text: str) -> str:
    """Mask anything that looks like a credential."""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the standard workflow fields on top."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("workflow_id", "stage", "role", "agent", "attempt", "transition", "latency_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO, *, path: Path | None = None, json_lines: bool = True) -> None:
    """Configure the ``reviewflow`` logger. Idempotent."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    formatter: logging.Formatter = JsonFormatter() if json_lines else logging.Formatter("%(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    logger.propagate = False


def get_logger(suffix: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{suffix}" if suffix else _LOGGER_NAME)
