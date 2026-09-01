"""Shared fixtures for ``tests/rate_limit/`` (Brief 531)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest


@dataclass
class MetricCall:
    method: str
    name: str
    value: float | int
    tags: dict[str, str] | None


class RecordingMetricsProvider:
    """In-memory ``MetricsProvider`` that captures every call.

    Mirrors the helper used by ``tests/auth/conftest.py`` (Brief 529)
    so the rate-limit tests can assert on emitted counters without
    cross-importing from a sibling test module.
    """

    kind: ClassVar[str] = "test"

    def __init__(self) -> None:
        self.calls: list[MetricCall] = []

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


@pytest.fixture
def recording_metrics() -> RecordingMetricsProvider:
    return RecordingMetricsProvider()
