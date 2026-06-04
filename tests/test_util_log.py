"""Logging helper smoke test."""
from __future__ import annotations

from concierge.util.log import configure_logging, get_logger


def test_configure_logging_and_get_logger():
    configure_logging("WARNING")
    log = get_logger("concierge.test")
    assert log.name == "concierge.test"
