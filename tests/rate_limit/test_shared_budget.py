"""REST + MCP share one ``route=broadcast`` rate-limit budget (T014, SC-010)."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp
from cassetta.mcp_server import set_sender_label

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


@pytest.fixture
def storage_dir() -> str:
    return tempfile.mkdtemp()


async def _booted_app(
    storage_dir: str,
    recording_metrics: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rate: str = "10/minute",
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    # Brief 539: monkeypatch auto-restores; previously this helper leaked
    # CASSETTA_PER_FILE_MAX/RATE_LIMIT_BROADCAST and broke capabilities/config
    # tests under randomized ordering (Principle IX).
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", storage_dir)
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.setenv("CASSETTA_PER_FILE_MAX", "1048576")
    monkeypatch.delenv("CASSETTA_MAX_FILE_SIZE", raising=False)
    monkeypatch.setenv(
        "CASSETTA_MCP_ALLOWED_HOSTS",
        "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001",
    )
    monkeypatch.setenv(
        "CASSETTA_JWT_KEY",
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0",
    )
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    monkeypatch.delenv("CASSETTA_KEYS_FILE", raising=False)
    monkeypatch.setenv("CASSETTA_RATE_LIMIT_BROADCAST", rate)

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    key_store = backends.key_store
    sender_raw, _ = await key_store.setup("test:sender")
    for i in range(3):
        await key_store.create_key(f"test:r{i}")

    backends = replace(backends, metrics_provider=recording_metrics)
    app.state.backends = backends
    configure_mcp(config, backends)

    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()
    set_sender_label("test:sender")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        yield client, sender_raw

    stop.set()
    await task


async def _init_mcp(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/mcp/",
        json=_jsonrpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "rl-test", "version": "1.0.0"},
            },
        ),
        headers=MCP_HEADERS,
    )
    return resp.headers.get("mcp-session-id", "")


async def _call_mcp_broadcast(
    client: httpx.AsyncClient,
    sid: str,
) -> dict:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    body = _jsonrpc(
        "tools/call",
        {
            "name": "cassetta_broadcast",
            "arguments": {"path": "x.txt", "content": "hi"},
        },
        req_id=2,
    )
    resp = await client.post("/mcp/", json=body, headers=headers)
    return resp.json()["result"]


class TestSharedBudget:
    @pytest.mark.asyncio
    async def test_rest_and_mcp_drain_one_bucket(
        self,
        storage_dir: str,
        recording_metrics: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async for client, sender in _booted_app(
            storage_dir,
            recording_metrics,
            monkeypatch,
            rate="3/minute",
        ):
            sid = await _init_mcp(client)

            # Two REST requests + one MCP call → exhaust the 3/min bucket.
            for _ in range(2):
                resp = await client.post(
                    "/broadcast/x.txt",
                    content=b"hi",
                    headers={"Authorization": f"Bearer {sender}"},
                )
                assert resp.status_code == 200, resp.text

            ok = await _call_mcp_broadcast(client, sid)
            assert ok.get("isError", False) is False, ok

            # 4th call (REST) should be rejected.
            resp = await client.post(
                "/broadcast/x.txt",
                content=b"hi",
                headers={"Authorization": f"Bearer {sender}"},
            )
            assert resp.status_code == 429
            body = resp.json()
            assert body["error"] == "rate_limit"

            # And so should an MCP call from the same client IP.
            rejected = await _call_mcp_broadcast(client, sid)
            assert rejected["isError"] is True
            text = rejected["content"][0]["text"]
            _, _, payload = text.partition(": ")
            mcp_body = json.loads(payload)
            assert mcp_body["error"] == "rate_limit"

            # SC-010: every increment carries the same surface-agnostic tag.
            calls = recording_metrics.find("cassetta.rate_limit.hits")
            assert len(calls) == 2
            for call in calls:
                assert call.tags == {"route": "broadcast", "reason": "rate"}
