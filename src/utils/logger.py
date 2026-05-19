"""
src/utils/logger.py
────────────────────────────────────────────────────────────────
Centralised logging configuration for the entire project.

All modules import `get_logger(__name__)` rather than calling
`logging.getLogger` directly. This gives us one place to control
formatting, log level, and output destinations — without touching
individual modules when we want to change behaviour.

We use Python's standard `logging` module rather than a third-party
library so there are zero extra dependencies for this foundational
piece.
────────────────────────────────────────────────────────────────
"""

import logging
import sys
from pathlib import Path


# ── Constants ────────────────────────────────────────────────────

# Log format: timestamp | level | module name | message
# The module name comes from the `name` argument passed to
# get_logger(), which should always be __name__.
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Optional: write logs to a file alongside stdout
LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


# ── Internal setup ───────────────────────────────────────────────

def _build_handler(stream) -> logging.StreamHandler:
    """Create a stream handler with the project's standard formatter."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    return handler


def _setup_root_logger(level: str = "INFO") -> None:
    """
    Configure the root logger once on first import.

    We configure the root logger so that any third-party library
    that uses logging also respects our format. We only do this
    once — subsequent calls to get_logger() just retrieve named
    child loggers.
    """
    root = logging.getLogger()

    # Avoid adding duplicate handlers if this function is called
    # more than once (e.g. during testing with module reloads).
    if root.handlers:
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric_level)

    # Always log to stdout so Docker / cloud platforms capture it
    root.addHandler(_build_handler(sys.stdout))


# Run setup when this module is first imported
_setup_root_logger()


# ── Public API ───────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger for a given module.

    Usage (in every module that needs logging):

        from src.utils.logger import get_logger
        logger = get_logger(__name__)
        logger.info("Loading data from %s", filepath)

    Parameters
    ----------
    name : str
        Typically pass __name__ so the log line shows the full
        dotted module path, e.g. 'src.etl.extract'.

    Returns
    -------
    logging.Logger
        A child logger that inherits the root configuration.
    """
    return logging.getLogger(name)


def set_log_level(level: str) -> None:
    """
    Adjust the log level at runtime without restarting.

    Useful when debugging a production issue: call
    set_log_level("DEBUG") from a management script.

    Parameters
    ----------
    level : str
        One of DEBUG, INFO, WARNING, ERROR, CRITICAL.
    """
    numeric_level = getattr(logging, level.upper(), None)
    if numeric_level is None:
        raise ValueError(f"Unknown log level: '{level}'")
    logging.getLogger().setLevel(numeric_level)
