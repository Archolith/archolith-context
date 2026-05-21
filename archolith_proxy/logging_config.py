"""Structured JSON logging configuration via structlog.

Configures structlog to emit JSON-formatted log lines suitable for
machine parsing (log aggregation, monitoring dashboards, grep).

Call configure_logging() once at application startup before any
structlog.get_logger() calls.

In test environments, set LOG_FORMAT=dev to get human-readable console
output instead of JSON.
"""

from __future__ import annotations

import logging
import os

import structlog


def configure_logging() -> None:
    """Configure structlog with JSON renderer (production) or dev console renderer.

    Uses stdlib.LoggerFactory so that structlog loggers are real stdlib
    logging.Logger instances — required by filter_by_level which checks
    the .disabled attribute that PrintLogger does not have.

    filter_by_level runs in the structlog processor chain (before
    wrap_for_formatter), not in ProcessorFormatter, because the
    formatter receives LogRecord objects where the logger reference
    may be None for stdlib-native log calls.
    """
    log_format = os.getenv("LOG_FORMAT", "json")

    if log_format == "dev":
        # Human-readable console output for development
        renderer = structlog.dev.ConsoleRenderer()
    else:
        # JSON output for production / log aggregation
        renderer = structlog.processors.JSONRenderer()

    # Configure structlog to use stdlib loggers (not PrintLogger).
    # filter_by_level requires a real logging.Logger with .disabled.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Apply the renderer as a formatter on the root logger.
    # Do NOT put filter_by_level here — it runs in the structlog chain above.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
