"""Unit tests for ``cassetta.rate_limit.limiter``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from slowapi import Limiter
from starlette.requests import Request

from cassetta.rate_limit import (
    FanoutCapExceeded,
    RateLimitExceeded,
    _check_fanout_cap,
    _record_rate_limit_hit,
    check_rate_limit_imperative,
    limiter,
)


def _request_with_metrics(metrics: object) -> Request:
    """Build a Starlette ``Request`` whose ``app.state.backends`` has metrics."""
    backends = SimpleNamespace(metrics_provider=metrics)
    state = SimpleNamespace(backends=backends)
    app = SimpleNamespace(state=state)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/broadcast",
        "headers": [],
        "client": ("198.51.100.7", 12345),
        "app": app,
    }
    return Request(scope)


class TestModuleSurface:
    def test_limiter_is_slowapi_instance(self) -> None:
        assert isinstance(limiter, Limiter)

    def test_limiter_uses_memory_storage(self) -> None:
        assert getattr(limiter, "_storage_uri", None) == "memory://"


class TestRecordRateLimitHit:
    def test_increments_counter_with_route_and_reason(
        self,
        recording_metrics: object,
    ) -> None:
        request = _request_with_metrics(recording_metrics)

        _record_rate_limit_hit(request, route="broadcast", reason="rate")

        calls = recording_metrics.find("cassetta.rate_limit.hits")  # type: ignore[attr-defined]
        assert len(calls) == 1
        assert calls[0].method == "increment"
        assert calls[0].tags == {"route": "broadcast", "reason": "rate"}

    def test_separate_calls_emit_separate_increments(
        self,
        recording_metrics: object,
    ) -> None:
        request = _request_with_metrics(recording_metrics)

        _record_rate_limit_hit(request, route="onboard", reason="rate")
        _record_rate_limit_hit(request, route="broadcast", reason="fanout_cap")

        calls = recording_metrics.find("cassetta.rate_limit.hits")  # type: ignore[attr-defined]
        assert len(calls) == 2
        assert calls[0].tags == {"route": "onboard", "reason": "rate"}
        assert calls[1].tags == {"route": "broadcast", "reason": "fanout_cap"}


class TestFanoutCap:
    def test_at_cap_returns_normally(self) -> None:
        _check_fanout_cap(target_count=1000, max_targets=1000)

    def test_below_cap_returns_normally(self) -> None:
        _check_fanout_cap(target_count=10, max_targets=1000)

    def test_above_cap_raises_with_max_targets(self) -> None:
        with pytest.raises(FanoutCapExceeded) as excinfo:
            _check_fanout_cap(target_count=1001, max_targets=1000)
        assert excinfo.value.max_targets == 1000


class TestImperativeCheck:
    def test_admits_until_bucket_full(self) -> None:
        # Fresh peer IP so this test is independent of the rest of the suite.
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "headers": [],
            "client": ("203.0.113.1", 0),
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
        request = Request(scope)

        # 2/minute — three calls; third should overflow.
        check_rate_limit_imperative(request, "2/minute", route="broadcast")
        check_rate_limit_imperative(request, "2/minute", route="broadcast")
        with pytest.raises(RateLimitExceeded):
            check_rate_limit_imperative(request, "2/minute", route="broadcast")
