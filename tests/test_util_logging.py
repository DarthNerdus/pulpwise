"""Tests for `util.logging.setup_logging`."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pulpwise.util.logging import (
    DEFAULT_FILENAME,
    LOGGER_NAME,
    default_log_dir,
    get_logger,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _reset_pulpwise_logger() -> None:
    """Tear down handlers between tests so each test starts clean."""
    logger = logging.getLogger(LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    logger.setLevel(logging.NOTSET)


def test_default_log_dir_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULPWISE_LOG_DIR", "/tmp/pulpwise-tests-logs")
    assert default_log_dir() == Path("/tmp/pulpwise-tests-logs")


def test_default_log_dir_falls_back_to_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULPWISE_LOG_DIR", raising=False)
    assert default_log_dir() == Path.home() / ".local" / "state" / "pulpwise" / "log"


def test_setup_logging_creates_directory_and_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "deeply" / "nested" / "log"
    path = setup_logging(log_dir=log_dir)

    assert path == log_dir / DEFAULT_FILENAME
    assert log_dir.is_dir()

    get_logger("test").info("hello world")
    for h in logging.getLogger(LOGGER_NAME).handlers:
        h.flush()

    assert path.exists()
    assert "hello world" in path.read_text(encoding="utf-8")


def test_setup_logging_is_idempotent_no_duplicate_records(tmp_path: Path) -> None:
    """Re-calling setup_logging in the same process must not double-write."""
    path = setup_logging(log_dir=tmp_path)
    setup_logging(log_dir=tmp_path)
    setup_logging(log_dir=tmp_path)

    get_logger("test").info("once")
    for h in logging.getLogger(LOGGER_NAME).handlers:
        h.flush()

    contents = path.read_text(encoding="utf-8")
    assert contents.count("once") == 1


def test_setup_logging_attaches_stderr_when_verbose(tmp_path: Path) -> None:
    setup_logging(verbose=True, log_dir=tmp_path)
    handlers = logging.getLogger(LOGGER_NAME).handlers
    stream_handlers = [
        h
        for h in handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert stream_handlers, "verbose=True should attach a StreamHandler to stderr"
    assert logging.getLogger(LOGGER_NAME).level == logging.DEBUG


def test_setup_logging_does_not_attach_stderr_when_quiet(tmp_path: Path) -> None:
    setup_logging(verbose=False, log_dir=tmp_path)
    handlers = logging.getLogger(LOGGER_NAME).handlers
    stream_handlers = [
        h
        for h in handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert not stream_handlers, "default setup should be file-only"


def test_get_logger_returns_pulpwise_child() -> None:
    logger = get_logger("pipeline")
    assert logger.name == f"{LOGGER_NAME}.pipeline"
