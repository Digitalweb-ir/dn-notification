from __future__ import annotations

import asyncio
import logging
import sys
from logging.config import dictConfig
from pathlib import Path

from .config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    level = settings.log_level.upper()

    # Ensure the logs directory exists before creating the file handler.
    logs_dir = Path(settings.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "app.log"

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
                "file": {
                    "level": level,
                    "formatter": "default",
                    "class": "logging.handlers.TimedRotatingFileHandler",
                    "filename": str(log_file),
                    "when": "midnight",
                    "interval": 1,
                    "backupCount": 7,
                    "encoding": "utf-8",
                    "utc": False,
                },
            },
            "loggers": {
                "": {"handlers": ["default", "file"], "level": level, "propagate": False},
                "uvicorn": {"handlers": ["default", "file"], "level": level, "propagate": False},
                "uvicorn.error": {"handlers": ["default", "file"], "level": level, "propagate": False},
                "uvicorn.access": {"handlers": ["default", "file"], "level": level, "propagate": False},
                "telethon": {"handlers": ["default", "file"], "level": "WARNING", "propagate": False},
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
