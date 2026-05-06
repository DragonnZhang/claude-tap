"""Logger configuration: levels respect verbosity, file handler attached."""

from __future__ import annotations

import logging

from claude_tap.logging_setup import configure_logging, get_logger


def _stderr_handler(logger):
    for h in logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            return h
    return None


def test_default_level_is_warning(tmp_path):
    log = configure_logging(verbosity=0, quiet=False)
    assert _stderr_handler(log).level == logging.WARNING


def test_v_increases_to_info(tmp_path):
    log = configure_logging(verbosity=1, quiet=False)
    assert _stderr_handler(log).level == logging.INFO


def test_vv_increases_to_debug(tmp_path):
    log = configure_logging(verbosity=2, quiet=False)
    assert _stderr_handler(log).level == logging.DEBUG


def test_quiet_overrides(tmp_path):
    log = configure_logging(verbosity=2, quiet=True)
    assert _stderr_handler(log).level == logging.ERROR


def test_log_file_captures_info_even_when_stderr_is_warning(tmp_path):
    """The whole point of the file handler: keep DEBUG/INFO that we hide
    from the TUI on stderr. Regression for an early bug where the logger
    level gated everything at WARNING."""
    log_file = tmp_path / "x.log"
    log = configure_logging(verbosity=0, quiet=False, log_file=log_file)
    log.info("hello-from-info")
    log.debug("hello-from-debug")
    for h in log.handlers:
        h.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "hello-from-info" in text
    assert "hello-from-debug" in text


def test_log_file_handler_attached(tmp_path):
    log_file = tmp_path / "x.log"
    log = configure_logging(verbosity=0, quiet=False, log_file=log_file)
    assert any(isinstance(h, logging.FileHandler) for h in log.handlers)
    assert log_file.exists()


def test_logger_does_not_propagate():
    """Required so the package does not pollute the root logger."""
    log = configure_logging(verbosity=0, quiet=False)
    assert log.propagate is False
    assert log is get_logger()
