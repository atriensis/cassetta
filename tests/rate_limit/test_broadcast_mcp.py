"""MCP ``cassetta_broadcast`` rate-limit + fan-out cap tests."""

from __future__ import annotations

import asyncio
import json
import os
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
    assert resp.status_code == 200
    return resp.headers.get("mcp-session-id", "")


async def _call_mcp(
    client: httpx.AsyncClient,
    sid: str,
    name: str,
    args: dict,
) -> dict:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    body = _jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2)
    resp = await client.post("/mcp/", json=body, headers=headers)
    return resp.json()["result"]


@pytest.fixture
def storage_dir() -> str:
    return tempfile.mkdtemp()


async def _booted_app(
    storage_dir: str,
    recording_metrics: Any,
    *,
    rate: str = "10/minute",
    max_targets: int = 1000,
    target_count: int = 3,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    os.environ["CASSETTA_SETUP_TOKEN"] = ""  # dev mode
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
    await key_store.setup("test:sender")
    for i in range(target_count):
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
    ) as c:
        yield c, app

    stop.set()
    await task


def _parse_error_text(result: dict) -> tuple[str, dict]:
    """Pluck the structured error JSON out of an MCP error result."""
    assert result["isError"] is True
    text = result["content"][0]["text"]
    # Format: "Error executing tool cassetta_broadcast: <json>"
    _, _, payload = text.partition(": ")
    return text, json.loads(payload)


class TestMcpBroadcastRateLimit:
    @pytest.mark.asyncio
    async def test_per_tool_call_429_envelope_via_mcp(
        self,
        storage_dir: str,
        recording_metrics: Any,
    ) -> None:
        async for client, _app in _booted_app(
            storage_dir,
            recording_metrics,
            rate="3/minute",
        ):
            sid = await _init_mcp(client)
            for _ in range(3):
                ok = await _call_mcp(
                    client,
                    sid,
                    "cassetta_broadcast",
                    {"path": "x.txt", "content": "hi"},
                )
                assert ok.get("isError", False) is False, ok

            rejected = await _call_mcp(
                client,
                sid,
                "cassetta_broadcast",
                {"path": "x.txt", "content": "hi"},
            )
            _, body = _parse_error_text(rejected)
            assert body["error"] == "rate_limit"
            assert isinstance(body["retry_after"], int)
            assert body["retry_after"] > 0

            calls = recording_metrics.find("cassetta.rate_limit.hits")
            assert len(calls) == 1
            assert calls[0].tags == {
                "route": "broadcast",
                "reason": "rate",
            }

    @pytest.mark.asyncio
    async def test_fanout_cap_via_mcp(
        self,
        storage_dir: str,
        recording_metrics: Any,
    ) -> None:
        async for client, _app in _booted_app(
            storage_dir,
            recording_metrics,
            rate="100/minute",
            max_targets=2,
            target_count=5,
        ):
            sid = await _init_mcp(client)
            rejected = await _call_mcp(
                client,
                sid,
                "cassetta_broadcast",
                {"path": "x.txt", "content": "hi"},
            )
            _, body = _parse_error_text(rejected)
            assert body == {
                "error": "rate_limit",
                "reason": "fanout_cap",
                "max_targets": 2,
            }
            calls = recording_metrics.find("cassetta.rate_limit.hits")
            assert len(calls) == 1
            assert calls[0].tags == {
                "route": "broadcast",
                "reason": "fanout_cap",
            }
