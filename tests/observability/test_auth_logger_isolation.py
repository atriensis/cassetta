"""Brief 533 FR-040/FR-041/FR-042 / SC-007 — auth logger isolation.

Also covers the other half of the same function's contract: the logger trees a
caller supplies, which are configured by this project but governed by whoever
supplied them.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from cassetta.structured_log import configure_logging

# A stand-in for a caller's own logger tree. It names no real distribution and
# is an example throughout — nothing here depends on who a supplied tree belongs
# to, which is the property these tests exist to pin.
_EXTRA_TREE = "example_embedder"


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Snapshot/restore the logger tree so each test runs hermetically."""
    yield
    for name in ("cassetta", "cassetta.auth", _EXTRA_TREE):
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


def test_extra_log_trees_receive_the_configured_handler() -> None:
    """A supplied tree is routed through the same handler as this project's own.

    Object identity, not type equality: one handler is built per call and
    attached everywhere, so a supplied tree holding that same object is the
    strongest available statement of "the same handler".
    """
    configure_logging("json", (_EXTRA_TREE,))

    own = logging.getLogger("cassetta")
    supplied = logging.getLogger(_EXTRA_TREE)

    assert supplied.handlers == own.handlers, "supplied tree did not receive the configured handler"
    assert supplied.level == own.level == logging.DEBUG

    # Re-route that handler to a buffer so we can read what reached it.
    stream = io.StringIO()
    for h in supplied.handlers:
        h.stream = stream  # type: ignore[attr-defined]

    logging.getLogger(f"{_EXTRA_TREE}.submodule").info(
        "supplied.test",
        extra={"event": "supplied.test"},
    )

    contents = stream.getvalue()
    assert contents, "expected at least one record on the configured handler"
    for line in contents.splitlines():
        obj = json.loads(line)
        assert obj.get("event") == "supplied.test"


def test_extra_log_trees_propagation_is_left_untouched() -> None:
    """A supplied tree's ``propagate`` stays whatever the caller left it as.

    Configuring a tree and governing it are different things: the handler and the
    level are this project's to set, propagation is not. Both directions are
    asserted — a default left alone, and an explicit ``False`` left alone — and
    the handler assertion in each half is what stops either from passing
    vacuously on a tree the function never visited.
    """
    supplied = logging.getLogger(_EXTRA_TREE)

    # Direction 1 — the caller never touched it: the default survives.
    assert supplied.propagate is True, "precondition: the tree starts at the logging default"
    configure_logging("text", (_EXTRA_TREE,))
    assert supplied.handlers, "the function never visited this tree"
    assert supplied.propagate is True, "propagation was disabled on a supplied tree"

    # Direction 2 — the caller set it themselves: their choice survives.
    supplied.propagate = False
    configure_logging("text", (_EXTRA_TREE,))
    assert supplied.handlers, "the function never visited this tree"
    assert supplied.propagate is False, "propagation was enabled on a supplied tree"
