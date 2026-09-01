"""Trusted-proxy XFF resolution → per-IP bucketing (T015, SC-011).

These tests focus on the cassetta-side contract: ``check_rate_limit_imperative``
buckets by what ``request.client.host`` carries at request time. The
end-to-end XFF substitution is handled by uvicorn's
``ProxyHeadersMiddleware`` outside the FastAPI app. We exercise that
middleware directly via Starlette's ASGI helpers — no real socket peer
required, and no need to spin up a uvicorn process.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from cassetta.rate_limit import (
    RateLimitExceeded,
    check_rate_limit_imperative,
)


def _build_request(*, client_ip: str, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/broadcast",
        "headers": headers or [],
        "client": (client_ip, 12345),
        "app": SimpleNamespace(state=SimpleNamespace()),
    }
    return Request(scope)


async def _resolve_via_proxy_headers(
    *, peer_ip: str, xff: str, trusted: str = "*",
) -> str:
    """Run ``ProxyHeadersMiddleware`` against a fake scope; return resolved IP."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/broadcast",
        "headers": [(b"x-forwarded-for", xff.encode())],
        "client": (peer_ip, 12345),
        "scheme": "http",
    }

    captured: dict[str, Any] = {}

    async def _inner_app(s: dict, _receive: Any, _send: Any) -> None:
        captured["scope"] = s

    async def _receive() -> dict:
        return {"type": "http.disconnect"}

    async def _send(_message: dict) -> None:
        pass

    middleware = ProxyHeadersMiddleware(_inner_app, trusted_hosts=trusted)
    await middleware(scope, _receive, _send)
    return captured["scope"]["client"][0]


class TestTrustedProxiesXFFResolution:
    @pytest.mark.asyncio
    async def test_xff_replaces_peer_when_proxy_trusted(self) -> None:
        ip = await _resolve_via_proxy_headers(
            peer_ip="10.0.0.5", xff="198.51.100.7", trusted="10.0.0.0/8",
        )
        assert ip == "198.51.100.7"

    @pytest.mark.asyncio
    async def test_xff_ignored_when_peer_untrusted(self) -> None:
        ip = await _resolve_via_proxy_headers(
            peer_ip="1.2.3.4", xff="198.51.100.7", trusted="10.0.0.0/8",
        )
        assert ip == "1.2.3.4"

    @pytest.mark.asyncio
    async def test_default_loopback_only(self) -> None:
        # uvicorn's default treats only 127.0.0.1 as a trusted proxy.
        ip = await _resolve_via_proxy_headers(
            peer_ip="10.0.0.5", xff="198.51.100.7", trusted="127.0.0.1",
        )
        assert ip == "10.0.0.5"


class TestPerIpBucketing:
    """Once XFF substitution lands the resolved IP on ``request.client.host``,
    cassetta's limiter buckets per that resolved value."""

    def test_two_distinct_ips_get_separate_buckets(self) -> None:
        a = _build_request(client_ip="198.51.100.7")
        b = _build_request(client_ip="198.51.100.8")
        # 1/minute — each IP can do exactly one before being told off.
        check_rate_limit_imperative(a, "1/minute")
        check_rate_limit_imperative(b, "1/minute")
        with pytest.raises(RateLimitExceeded):
            check_rate_limit_imperative(a, "1/minute")
        with pytest.raises(RateLimitExceeded):
            check_rate_limit_imperative(b, "1/minute")

    def test_same_ip_shares_bucket_across_calls(self) -> None:
        peer = _build_request(client_ip="198.51.100.42")
        check_rate_limit_imperative(peer, "1/minute")
        with pytest.raises(RateLimitExceeded):
            check_rate_limit_imperative(peer, "1/minute")
