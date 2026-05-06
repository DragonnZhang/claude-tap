"""Centralised logging configuration that does not pollute the root logger."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOGGER_NAME = "claude_tap"


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def configure_logging(*, verbosity: int, quiet: bool, log_file: Path | None = None) -> logging.Logger:
    """Configure the package logger.

    verbosity counts ``-v`` flags (0=WARNING, 1=INFO, 2+=DEBUG). ``quiet`` forces
    ERROR level. ``log_file`` adds a file handler (used by run/proxy commands so
    the TUI is not spammed with proxy noise).
    """

    logger = get_logger()
    logger.handlers.clear()
    logger.propagate = False

    if quiet:
        stderr_level = logging.ERROR
    elif verbosity >= 2:
        stderr_level = logging.DEBUG
    elif verbosity == 1:
        stderr_level = logging.INFO
    else:
        stderr_level = logging.WARNING

    # Logger level must be the *lowest* of any handler's level so messages
    # destined for the file aren't dropped at the logger gate.
    logger.setLevel(logging.DEBUG if log_file is not None else stderr_level)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    stderr.setLevel(stderr_level)
    logger.addHandler(stderr)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)

        # Capture aiohttp.server crashes to the file too, but keep them off the TUI.
        aio = logging.getLogger("aiohttp.server")
        aio.handlers.clear()
        aio.addHandler(fh)
        aio.propagate = False

    # Always silence aiohttp access logs on stderr.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    return logger
