"""Layer-1 protocol — Metrics extension point.

Core defines `MetricsProvider` (Protocol). Cloud or third-party deployments
plug a real implementation (e.g. Prometheus, StatsD, DataDog).
The default no-op implementation `DefaultMetricsProvider` lives in
`cassetta.defaults.default_metrics`.

Core emits metric calls at defined hook points (request counts, durations,
error rates, storage operations, policy decisions, etc.). The no-op default
ensures zero overhead when metrics collection is not configured.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable


@runtime_checkable
class MetricsProvider(Protocol):
    """Strategy interface for metrics collection.

    Three metric types:
    - ``increment``: monotonically increasing counters
    - ``observe``: histogram / distribution observations
    - ``gauge``: point-in-time values that can go up or down

    All methods are synchronous — real metrics SDKs (Prometheus, StatsD)
    use synchronous client libraries.  Tags enable dimensional filtering.
    """

    kind: ClassVar[str]

    def increment(
        self,
        name: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None: ...

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
