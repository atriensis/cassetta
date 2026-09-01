"""Brief 533 FR-040/FR-041/FR-042 / SC-007 — auth logger isolation."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from cassetta.structured_log import configure_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Snapshot/restore the logger tree so each test runs hermetically."""
    yield
    for name in ("cassetta", "cassetta.auth", "cassetta_cloud"):
        target = logging.getLogger(name)
        for h in list(target.handlers):
            target.removeHandler(h)
        target.setLevel(logging.NOTSET)
        target.propagate = True


def test_auth_record_renders_through_configured_handler() -> None:
    """FR-040 / SC-007 — record on ``cassetta.auth.observability``
    renders through the configured ``JsonFormatter``.
    """
    # Re-route the StreamHandler to a StringIO so we can read what was
    # written.
    stream = io.StringIO()
    configure_logging("json")
    cassetta_logger = logging.getLogger("cassetta")
    for h in cassetta_logger.handlers:
        h.stream = stream  # type: ignore[attr-defined]
    # The auth sub-tree handler MUST also point at our stream so we can
    # inspect what reached it.
    auth_logger = logging.getLogger("cassetta.auth")
    for h in auth_logger.handlers:
        h.stream = stream  # type: ignore[attr-defined]

    logging.getLogger("cassetta.auth.observability").info(
        "auth.test",
        extra={"event": "auth.test"},
    )

    contents = stream.getvalue()
    assert contents, "expected at least one record on the configured handler"
    # Each line should parse as JSON.
    for line in contents.splitlines():
        obj = json.loads(line)
        assert obj.get("event") == "auth.test"


def test_auth_record_does_not_propagate_to_root() -> None:
    """FR-041 / SC-007 — a recording handler on root MUST NOT receive
    any ``cassetta.auth.*`` record after ``configure_logging``.
    """
    configure_logging("text")

    captured: list[logging.LogRecord] = []

    class _RootHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name.startswith("cassetta.auth"):
                captured.append(record)

    root = logging.getLogger()
    handler = _RootHandler()
    root.addHandler(handler)
    try:
        logging.getLogger("cassetta.auth.observability").info("noprop.test")
    finally:
        root.removeHandler(handler)

    assert captured == [], f"cassetta.auth records propagated to root: {[r.msg for r in captured]}"


def test_configure_logging_idempotent_on_auth_subtree() -> None:
    """FR-042 / SC-007 — re-invoking configure_logging does not duplicate
    handlers or duplicate emissions on the ``cassetta.auth`` sub-tree.
    """
    configure_logging("text")
    configure_logging("text")

    auth_logger = logging.getLogger("cassetta.auth")
    # Idempotent: exactly one handler attached after re-invocation.
    assert len(auth_logger.handlers) == 1, [type(h).__name__ for h in auth_logger.handlers]
    # No propagation, so a root recording handler sees no record.
    captured: list[logging.LogRecord] = []

    class _RootHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name.startswith("cassetta.auth"):
                captured.append(record)

    root = logging.getLogger()
    h = _RootHandler()
    root.addHandler(h)
    try:
        logging.getLogger("cassetta.auth.observability").info("once")
    finally:
        root.removeHandler(h)

    assert len(captured) == 0


def test_cassetta_cloud_propagate_unchanged() -> None:
    """D-6 — ``cassetta_cloud`` propagation behavior UNCHANGED from
    brief 529 (``propagate=True`` preserved for caplog compatibility).
    """
    configure_logging("text")
    cloud_logger = logging.getLogger("cassetta_cloud")
    assert cloud_logger.propagate is True
