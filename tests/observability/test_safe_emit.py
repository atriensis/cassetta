"""Tests for ``cassetta.structured_log.safe_emit`` (Brief 533).

Locks the contract pinned in
``specs/533-observability-completeness/contracts/safe_emit.md``:

- C-SAFEEMIT-002 / FR-061: log + metric steps are independent best-effort.
- SC-009: metric-step failure does NOT block the request; fallback ERROR
  line is logged through ``logger.exception``.
- SC-010: log-step failure does NOT block the metric step.
- FR-061 catastrophic case: both ``struct_log`` AND the fallback ``logger.exception``
  raising MUST NOT propagate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class _Call:
    name: str
    value: float
    tags: dict[str, str] | None


@dataclass
class _Recorder:
    """Minimal MetricsProvider double — captures every call."""

    kind: ClassVar[str] = "test"
    increments: list[_Call] = field(default_factory=list)
    gauges: list[_Call] = field(default_factory=list)
    observes: list[_Call] = field(default_factory=list)

    def increment(
        self,
        name: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.increments.append(_Call(name, value, tags))

    def observe(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.observes.append(_Call(name, value, tags))

    def gauge(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.gauges.append(_Call(name, value, tags))


def _attach_capture(
    logger_name: str,
) -> tuple[list[logging.LogRecord], logging.Logger, logging.Handler]:
    captured: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Handler()
    handler.setLevel(logging.DEBUG)
    target = logging.getLogger(logger_name)
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    return captured, target, handler


def test_log_step_failure_does_not_block_metric(
    monkeypatch,
) -> None:
    """SC-010 / FR-061 — struct_log raising MUST NOT prevent the increment."""
    from cassetta import structured_log as sl_mod

    def boom_struct_log(*_a: object, **_kw: object) -> None:
        raise RuntimeError("logging down")

    monkeypatch.setattr(sl_mod, "struct_log", boom_struct_log)

    metrics = _Recorder()
    captured, target, handler = _attach_capture("cassetta.test_safe_emit_a")
    try:
        sl_mod.safe_emit(
            target,
            logging.INFO,
            "test.event",
            metric_name="cassetta.test.counter",
            metric_tags={"k": "v"},
            metrics=metrics,
        )
    finally:
        target.removeHandler(handler)

    assert len(metrics.increments) == 1
    assert metrics.increments[0].name == "cassetta.test.counter"
    assert metrics.increments[0].tags == {"k": "v"}
    fallbacks = [r for r in captured if "struct_log failed" in r.getMessage()]
    assert len(fallbacks) == 1
    assert fallbacks[0].levelno == logging.ERROR


def test_metric_step_failure_does_not_block_request() -> None:
    """SC-009 / FR-061 — metric raising MUST NOT propagate; fallback logged."""
    from cassetta import structured_log as sl_mod

    class BoomProvider:
        kind = "boom"

        def increment(
            self,
            name: str,
            value: int = 1,
            tags: dict[str, str] | None = None,
        ) -> None:
            raise RuntimeError("metrics down")

        def observe(
            self,
            name: str,
            value: float,
            tags: dict[str, str] | None = None,
        ) -> None: ...

        def gauge(
            self,
            name: str,
            value: float,
            tags: dict[str, str] | None = None,
        ) -> None: ...

    metrics = BoomProvider()
    captured, target, handler = _attach_capture("cassetta.test_safe_emit_b")
    try:
        sl_mod.safe_emit(
            target,
            logging.INFO,
            "test.event",
            metric_name="cassetta.test.counter",
            metrics=metrics,
        )
    finally:
        target.removeHandler(handler)

    events = [r for r in captured if getattr(r, "event", None) == "test.event"]
    assert len(events) == 1
    fallbacks = [r for r in captured if "metrics.increment failed" in r.getMessage()]
    assert len(fallbacks) == 1
    assert fallbacks[0].levelno == logging.ERROR


def test_catastrophic_broken_logger(monkeypatch) -> None:
    """FR-061 catastrophic case — broken struct_log AND broken logger.exception
    MUST NOT propagate.
    """
    from cassetta import structured_log as sl_mod

    def boom_struct_log(*_a: object, **_kw: object) -> None:
        raise RuntimeError("logging down")

    monkeypatch.setattr(sl_mod, "struct_log", boom_struct_log)

    class BoomLogger:
        def exception(self, *_a: object, **_kw: object) -> None:
            raise RuntimeError("catastrophic")

        def warning(self, *_a: object, **_kw: object) -> None: ...

        def log(self, *_a: object, **_kw: object) -> None: ...

    metrics = _Recorder()

    # MUST NOT raise.
    sl_mod.safe_emit(
        BoomLogger(),  # type: ignore[arg-type]
        logging.INFO,
        "test.event",
        metric_name="cassetta.test.counter",
        metrics=metrics,
    )
    # Metric step still executes.
    assert len(metrics.increments) == 1


def test_gauge_mode_routes_to_metrics_gauge() -> None:
    """``gauge=True`` MUST call ``metrics.gauge`` instead of ``metrics.increment``."""
    from cassetta import structured_log as sl_mod

    metrics = _Recorder()
    sl_mod.safe_emit(
        logging.getLogger("cassetta.test_safe_emit_c"),
        logging.INFO,
        None,
        metric_name="cassetta.active_keys",
        metric_value=5.0,
        metrics=metrics,
        gauge=True,
    )
    assert len(metrics.increments) == 0
    assert len(metrics.gauges) == 1
    assert metrics.gauges[0].name == "cassetta.active_keys"
    assert metrics.gauges[0].value == 5.0


def test_log_only_skips_metric_when_metric_name_none() -> None:
    """Skip the metric step when ``metric_name is None``."""
    from cassetta import structured_log as sl_mod

    metrics = _Recorder()
    captured, target, handler = _attach_capture("cassetta.test_safe_emit_d")
    try:
        sl_mod.safe_emit(
            target,
            logging.INFO,
            "log.only",
            metrics=metrics,
        )
    finally:
        target.removeHandler(handler)
    assert len(metrics.increments) == 0
    assert any(getattr(r, "event", None) == "log.only" for r in captured)


def test_metric_only_skips_log_when_event_none() -> None:
    """Skip the log step when ``event is None``."""
    from cassetta import structured_log as sl_mod

    metrics = _Recorder()
    captured, target, handler = _attach_capture("cassetta.test_safe_emit_e")
    try:
        sl_mod.safe_emit(
            target,
            logging.INFO,
            None,
            metric_name="cassetta.test.counter",
            metrics=metrics,
        )
    finally:
        target.removeHandler(handler)
    assert len(metrics.increments) == 1
    assert not any(getattr(r, "event", None) is not None for r in captured)
