"""REST ``/broadcast`` rate-limit + fan-out cap integration tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import replace
from typing import Any

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp


@pytest.fixture
def storage_dir() -> str:
    return tempfile.mkdtemp()


async def _start_mcp(app: Any) -> Any:
    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()

    async def _stop() -> None:
        stop.set()
        await task

    return _stop


async def _booted_app(
    storage_dir: str,
    recording_metrics: Any,
    *,
    rate: str = "10/minute",
    max_targets: int = 1000,
    target_count: int = 3,
) -> tuple[httpx.AsyncClient, Any, str, Any]:
    """Boot a fresh app with custom rate-limit env, populate active keys."""
    os.environ["CASSETTA_SETUP_TOKEN"] = "rl-test-token"
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_PER_FILE_MAX"] = "1048576"
    os.environ.pop("CASSETTA_MAX_FILE_SIZE", None)
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    os.environ["CASSETTA_RATE_LIMIT_BROADCAST"] = rate
    os.environ["CASSETTA_BROADCAST_MAX_TARGETS"] = str(max_targets)

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    key_store = backends.key_store

    sender_raw, _ = await key_store.setup("test:sender")
    for i in range(target_count):
        await key_store.create_key(f"test:r{i}")

    backends = replace(backends, metrics_provider=recording_metrics)
    app.state.backends = backends
    configure_mcp(config, backends)

    stop_mcp = await _start_mcp(app)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    )
    return client, app, sender_raw, stop_mcp


class TestRestBroadcastRateLimit:
    @pytest.mark.asyncio
    async def test_429_envelope_shape_after_budget_exceeded(
        self,
        storage_dir: str,
        recording_metrics: Any,
    ) -> None:
        client, _app, sender, stop_mcp = await _booted_app(
            storage_dir,
            recording_metrics,
            rate="3/minute",
        )
        try:
            for _ in range(3):
                resp = await client.post(
                    "/broadcast/x.txt",
                    content=b"hi",
                    headers={"Authorization": f"Bearer {sender}"},
                )
                assert resp.status_code == 200, resp.text

            resp = await client.post(
                "/broadcast/x.txt",
                content=b"hi",
                headers={"Authorization": f"Bearer {sender}"},
            )
            assert resp.status_code == 429
            body = resp.json()
            assert body["error"] == "rate_limit"
            assert isinstance(body["retry_after"], int)
            assert body["retry_after"] > 0
            assert resp.headers["retry-after"] == str(body["retry_after"])

            calls = recording_metrics.find("cassetta.rate_limit.hits")
            assert len(calls) == 1
            assert calls[0].tags == {
                "route": "broadcast",
                "reason": "rate",
            }
        finally:
            await client.aclose()
            await stop_mcp()

    @pytest.mark.asyncio
    async def test_fanout_cap_envelope_blocks_before_storage_write(
        self,
        storage_dir: str,
        recording_metrics: Any,
    ) -> None:
        client, app, sender, stop_mcp = await _booted_app(
            storage_dir,
            recording_metrics,
            rate="100/minute",
            max_targets=2,
            target_count=5,
        )
        try:
            backend = app.state.backends.backend
            opens: list[str] = []
            original_open = backend.open_bundle_write

            async def spy_open(path: str) -> Any:
                opens.append(path)
                return await original_open(path)

            backend.open_bundle_write = spy_open  # type: ignore[method-assign]

            resp = await client.post(
                "/broadcast/x.txt",
                content=b"hi",
                headers={"Authorization": f"Bearer {sender}"},
            )
            assert resp.status_code == 429, resp.text
            body = resp.json()
            assert body == {
                "error": "rate_limit",
                "reason": "fanout_cap",
                "max_targets": 2,
            }
            assert resp.headers["retry-after"] == "0"

            assert opens == [], "Fan-out cap rejection must precede every storage write."

            calls = recording_metrics.find("cassetta.rate_limit.hits")
            assert len(calls) == 1
            assert calls[0].tags == {
                "route": "broadcast",
                "reason": "fanout_cap",
            }
        finally:
            await client.aclose()
            await stop_mcp()

    @pytest.mark.asyncio
    async def test_at_cap_proceeds_no_counter_advance(
        self,
        storage_dir: str,
        recording_metrics: Any,
    ) -> None:
        client, _app, sender, stop_mcp = await _booted_app(
            storage_dir,
            recording_metrics,
            rate="100/minute",
            max_targets=5,
            target_count=5,
        )
        try:
            resp = await client.post(
                "/broadcast/x.txt",
                content=b"hi",
                headers={"Authorization": f"Bearer {sender}"},
            )
            assert resp.status_code == 200, resp.text

            assert recording_metrics.find("cassetta.rate_limit.hits") == []
        finally:
            await client.aclose()
            await stop_mcp()
