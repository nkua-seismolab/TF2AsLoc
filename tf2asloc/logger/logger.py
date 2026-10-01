"""Logging helpers for TF2AsLoc.

Use ``logging.getLogger(__name__)`` in each module.  Call
``setup_logging()`` once at process startup (the API and Orchestrator
entry points do this automatically).
"""

import logging
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(
    log_dir: Path = Path("logs"),
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    when: str = "midnight",
    backup_count: int = 14,
) -> None:
    """Configure the root logger with a console handler and a timed rotating
    file handler.  Safe to call multiple times - additional calls are no-ops.
    """
    root = logging.getLogger()
    if root.handlers:
        return  # Already configured

    root.setLevel(min(console_level, file_level))
    fmt = logging.Formatter(_LOG_FORMAT)

    sh = logging.StreamHandler()
    sh.setLevel(console_level)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    log_dir.mkdir(exist_ok=True, parents=True)
    fh = TimedRotatingFileHandler(
        log_dir / "tf2asloc.log",
        when=when,
        backupCount=backup_count,
        encoding="utf-8",
    )
    fh.setLevel(file_level)
    fh.setFormatter(fmt)
    root.addHandler(fh)


# ---------------------------------------------------------------------------
# Legacy helpers - kept for backwards compatibility
# ---------------------------------------------------------------------------


def init_logger(
    name: str,
    log_dir: Path = Path("logs"),
    level: int = logging.INFO,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    max_bytes: int = 2_000_000,
    backup_count: int = 3,
) -> logging.Logger:

    log_dir.mkdir(exist_ok=True, parents=True)
    logger = logging.getLogger(name)
    if logger.handlers:  # already initialised
        return logger

    # Set logger to the most permissive level to allow all handlers to filter
    logger.setLevel(min(console_level, file_level))
    fmt = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    formatter = logging.Formatter(fmt)

    # Console handler - INFO level to reduce clutter
    sh = logging.StreamHandler()
    sh.setLevel(console_level)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    # File handler - DEBUG level to capture all details
    fh = RotatingFileHandler(
        log_dir / f"{name}.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    fh.setLevel(file_level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    logger.propagate = False
    return logger


def get_logger(
    name: str,
    log_dir: Path = Path("logs"),
    level: int = logging.INFO,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    max_bytes: int = 2_000_000,
    backup_count: int = 3,
) -> logging.Logger:
    """
    Get a logger with both console and file output.
    This is a wrapper around init_logger for backward compatibility.

    Args:
        name: Logger name (usually __name__)
        log_dir: Directory to store log files
        level: Logging level (for backward compatibility, maps to console_level)
        console_level: Logging level for console output (default: INFO)
        file_level: Logging level for file output (default: DEBUG)
        max_bytes: Maximum size of each log file before rotation
        backup_count: Number of backup files to keep

    Returns:
        Configured logger instance
    """
    return init_logger(name, log_dir, level, console_level, file_level, max_bytes, backup_count)
