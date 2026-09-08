"""A rate-limit rejection is counted against the route its caller named.

The 429 handler used to decide which counter a hit belonged to by reading
``request.url.path.startswith("/onboard")`` — a path this repository does not serve, hardcoded in
its exception handler. The caller that does serve it could not be counted correctly, and the caller
that does not could not say so.

The seam is inverted now: whoever drives the limiter names its own route, the handler reads what was
recorded. Both halves are held here, and the second one — the fallback — is the compatibility
assertion, because nothing in this repository exercises it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from cassetta.app import create_app
from cassetta.config import AppConfig
from cassetta.rate_limit import (
    RateLimitExceeded,
    check_rate_limit_imperative,
    recorded_rate_limit_route,
)

_TEST_JWT_KEY = b"test-test-test-test-test-test-test-t"


@dataclass
class _MetricCall:
    name: str
    tags: dict[str, str] | None


class _Recorder:
    """The smallest ``MetricsProvider`` that can answer "what was this hit tagged with"."""

    kind = "test"

    def __init__(self) -> None:
        self.calls: list[_MetricCall] = []

    def increment(self, name: str, value: int = 1, tags: dict[str, str] | None = None) -> None:
        self.calls.append(_MetricCall(name, tags))

    def observe(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        self.calls.append(_MetricCall(name, tags))

    def gauge(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        self.calls.append(_MetricCall(name, tags))

    def route_tags(self) -> list[str | None]:
        return [c.tags.get("route") if c.tags else None for c in self.calls if c.name == "cassetta.rate_limit.hits"]


def _scope(client_ip: str, path: str) -> dict[str, Any]:
    """An HTTP scope carrying a metrics recorder, and a client address of the test's own.

    The address is a parameter because the limiter's buckets are keyed on it: two tests sharing one
    address would share one bucket, and the second would depend on how many hits the first spent
    (Principle IX).
    """
    recorder = _Recorder()
    app = SimpleNamespace(state=SimpleNamespace(backends=SimpleNamespace(metrics_provider=recorder)))
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "client": (client_ip, 12345),
        "app": app,
    }


def _recorder_of(scope: dict[str, Any]) -> _Recorder:
    recorder: _Recorder = scope["app"].state.backends.metrics_provider
    return recorder


def _rate_limit_handler(tmp_path: Path) -> Any:
    """The application's own ``RateLimitExceeded`` handler, reached as a function.

    Registered by ``create_app`` and pulled back out of the registry rather than exercised through a
    live server: the subject is which counter the handler picks, and a socket would add a great deal
    of machinery to answer that.
    """
    config = AppConfig(
        setup_token="",
        dev_mode=True,
        storage_path=str(tmp_path / "data"),
        keys_file=str(tmp_path / "data.keys" / ".cassetta-keys.json"),
        default_ttl=0,
        allowed_path_chars=r"a-zA-Z0-9\-_./",
        mcp_allowed_hosts=(),
        jwt_primary_key=_TEST_JWT_KEY,
        public_base_url="http://localhost:16001",
    )
    app = create_app(config)
    return app.exception_handlers[RateLimitExceeded]


async def test_a_hit_is_attributed_to_the_route_the_caller_named(tmp_path: Path) -> None:
    """A rejection driven through the helper carries the caller's own route.

    ``onboard`` is chosen deliberately: this repository serves no such path, so the attribution can
    only have come from what the caller said. The handler this replaced would have read the request
    path — ``/x`` here — and counted the hit as ``broadcast``.
    """
    scope = _scope("203.0.113.10", "/x")
    request = Request(scope)

    check_rate_limit_imperative(request, "1/minute", route="onboard")
    assert recorded_rate_limit_route(request) == "onboard"

    with pytest.raises(RateLimitExceeded) as excinfo:
        check_rate_limit_imperative(request, "1/minute", route="onboard")

    # Starlette builds its own Request for the same scope before calling the handler; the recorded
    # route travels on the scope, which is what lets the two see the same value.
    handler = _rate_limit_handler(tmp_path)
    response = await handler(Request(scope), excinfo.value)

    assert response.status_code == 429
    assert _recorder_of(scope).route_tags() == ["onboard"]


async def test_a_hit_with_nothing_recorded_is_attributed_to_broadcast(tmp_path: Path) -> None:
    """A rejection that never passed through the helper is still counted, as ``broadcast``.

    This is what the old ``else`` branch did, and it must keep doing it. A ``@limiter.limit``
    decorator raises without going near the imperative entry point, so it records nothing — and a
    decorator is exactly what an application embedding this library installs on its own routes.
    Nothing in this repository takes that path, which is why it is asserted rather than observed.
    """
    # Any rejection will do; this one is raised elsewhere, on a request of its own, so the one
    # handed to the handler below has genuinely never seen the helper.
    driving = Request(_scope("203.0.113.11", "/x"))
    check_rate_limit_imperative(driving, "1/minute", route="onboard")
    with pytest.raises(RateLimitExceeded) as excinfo:
        check_rate_limit_imperative(driving, "1/minute", route="onboard")

    untouched = _scope("203.0.113.12", "/onboard/whatever")
    assert recorded_rate_limit_route(Request(untouched)) == "broadcast"

    handler = _rate_limit_handler(tmp_path)
    response = await handler(Request(untouched), excinfo.value)

    assert response.status_code == 429
    assert _recorder_of(untouched).route_tags() == ["broadcast"]
