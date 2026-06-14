from __future__ import annotations

import asyncio
import logging
import sys
from logging.config import dictConfig

from .config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    level = settings.log_level.upper()

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                },
                "access": {
                    "format": "%(asctime)s [%(access)s] %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                },
            },
            "handlers": {
                "default": {
                    "level": level,
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                },
            },
            "loggers": {
                "": {"handlers": ["default"], "level": level, "propagate": False},
                "uvicorn": {"handlers": ["default"], "level": level, "propagate": False},
                "uvicorn.error": {"handlers": ["default"], "level": level, "propagate": False},
                "uvicorn.access": {"handlers": ["default"], "level": level, "propagate": False},
                "telethon": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            },
        }
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# Default logger for the package
logger = get_logger("telegram_service")


async def shutdown_logging() -> None:
    """Flush handlers cleanly on shutdown."""
    await asyncio.sleep(0)
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass
