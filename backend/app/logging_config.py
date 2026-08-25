from __future__ import annotations

import logging

import structlog

from .config import Settings


def configure_logging(settings: Settings) -> None:
    """Emit JSON for Cloud Logging and readable key/value records for local demos."""

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.app_env.lower() in {"cloud", "production"}
        else structlog.processors.KeyValueRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=logging.INFO, format="%(message)s")
