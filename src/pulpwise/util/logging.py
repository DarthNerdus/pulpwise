"""File-based logging at `~/.local/state/pulpwise/log/pulpwise.log`.

Default behavior: log to file only; stderr stays clean. `pulpwise sync`
runs under cron benefit from durable logs even when their stderr is
captured into mail or `/dev/null`. INFO level by default; DEBUG when `--verbose`
is passed at the CLI.

The shape of `pulpwise.<module>` loggers mirrors the package layout, so
`get_logger("pipeline")` writes records tagged `pulpwise.pipeline`.

Rotation is by size (1MB per file, 5 backups) rather than time, so a
quiet week doesn't leave us with a stale handful of empty rotated logs.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

LOGGER_NAME = "pulpwise"
DEFAULT_FILENAME = "pulpwise.log"
_MAX_BYTES = 1_000_000  # 1 MB per file
_BACKUP_COUNT = 5  # so up to 6 MB of logs retained
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_STDERR_FORMAT = "%(levelname)s %(name)s: %(message)s"


def default_log_dir() -> Path:
    """XDG state dir for logs. `PULPWISE_LOG_DIR` env var overrides."""
    override = os.environ.get("PULPWISE_LOG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / "pulpwise" / "log"


def setup_logging(verbose: bool = False, log_dir: Path | None = None) -> Path:
    """Wire up file logging on the `pulpwise` logger.

    Idempotent: re-calling clears the previously installed pulpwise
    handlers before re-attaching, so repeated calls in tests or in the
    REPL don't double-write each record.

    Returns the resolved log file path so callers can surface it.
    """
    target_dir = (log_dir or default_log_dir()).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / DEFAULT_FILENAME

    logger = logging.getLogger(LOGGER_NAME)
    # Clear handlers we may have attached on a previous setup_logging()
    # call. Other libraries' handlers on the same logger object stay put.
    for existing in list(logger.handlers):
        if getattr(existing, "_pulpwise_owned", False):
            logger.removeHandler(existing)
            existing.close()

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    file_handler._pulpwise_owned = True  # type: ignore[attr-defined]
    logger.addHandler(file_handler)

    if verbose:
        stderr_handler = logging.StreamHandler()
        stderr_handler.setFormatter(logging.Formatter(_STDERR_FORMAT))
        stderr_handler._pulpwise_owned = True  # type: ignore[attr-defined]
        logger.addHandler(stderr_handler)

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Don't propagate to root - root logger may have its own handlers
    # (pytest installs one) and we don't want our records duplicated.
    logger.propagate = False

    return log_path


def get_logger(module: str) -> logging.Logger:
    """Return a child logger of `pulpwise.<module>`."""
    return logging.getLogger(f"{LOGGER_NAME}.{module}")
