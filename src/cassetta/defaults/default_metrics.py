"""Layer-2 default — `DefaultMetricsProvider`.

No-op metrics provider. All methods do nothing — zero overhead when metrics
collection is not configured. This preserves existing behavior for core-only
and Pi deployments.
"""

from __future__ import annotations

from typing import ClassVar


class DefaultMetricsProvider:
    """No-op metrics provider — the core default."""

    kind: ClassVar[str] = "core"

    def increment(
        self,
        name: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        pass

    def observe(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        pass

    def gauge(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        pass
