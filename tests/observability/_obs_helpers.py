"""Shared test helpers for ``core/tests/observability/`` (Brief 533).

Lives next to ``conftest.py`` (no ``__init__.py`` in the directory, per
project convention) and is exposed via a ``sys.path`` injection in the
sibling ``conftest.py`` so test modules can ``from _obs_helpers import …``
without depending on relative-package imports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class MetricCall:
    method: str
    name: str
    value: float | int
    tags: dict[str, str] | None


@dataclass
class RecordingMetricsProvider:
    """Test ``MetricsProvider`` that records every call."""

    kind: ClassVar[str] = "test"
    calls: list[MetricCall] = field(default_factory=list)

    def increment(
        self, name: str, value: int = 1, tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(MetricCall("increment", name, value, tags))

    def observe(
        self, name: str, value: float, tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(MetricCall("observe", name, value, tags))

    def gauge(
        self, name: str, value: float, tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(MetricCall("gauge", name, value, tags))

    def find(
        self, name: str, method: str | None = None,
    ) -> list[MetricCall]:
        return [
            c for c in self.calls
            if c.name == name and (method is None or c.method == method)
        ]

    def has(self, name: str, method: str | None = None) -> bool:
        return len(self.find(name, method)) > 0


class CassettaLogCapture:
    """Helper that attaches a recording handler to the ``cassetta`` logger.

    Call ``.attach()`` AFTER ``create_app`` (which clears handlers via
    ``configure_logging``); call ``.detach()`` for cleanup. The captured
    records are exposed as ``self.records``.
    """

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self._handler: logging.Handler | None = None
        self._prior_level: int = logging.NOTSET
        self._logger_name: str = "cassetta"

    def attach(self, logger_name: str = "cassetta") -> None:
        captured = self.records

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        self._handler = _Handler()
        self._handler.setLevel(logging.DEBUG)
        target = logging.getLogger(logger_name)
        target.addHandler(self._handler)
        self._prior_level = target.level
        target.setLevel(logging.DEBUG)
        self._logger_name = logger_name

    def detach(self) -> None:
        if self._handler is not None:
            target = logging.getLogger(self._logger_name)
            target.removeHandler(self._handler)
            target.setLevel(self._prior_level)
            self._handler = None

    def clear(self) -> None:
        self.records.clear()
