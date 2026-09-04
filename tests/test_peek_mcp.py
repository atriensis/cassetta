"""Integration tests for the cassetta_peek MCP tool."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp
from cassetta.mcp_server import set_sender_label

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
def peek_storage_dir() -> str:
    return tempfile.mkdtemp()


@pytest.fixture
def peek_env(peek_storage_dir: str) -> None:
    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = peek_storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)


@pytest.fixture
async def mcp_peek(peek_env: None, peek_storage_dir: str) -> AsyncIterator[tuple]:
    app = create_app()
    config = app.state.config
    backends = app.state.backends
    backend = backends.backend
    configure_mcp(config, backends)

    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        yield app, client, backend

    stop.set()
    await task


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


async def _post(
    client: httpx.AsyncClient,
    body: dict,
    sid: str = "",
) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    return await client.post("/mcp/", json=body, headers=headers)


async def _init(client: httpx.AsyncClient) -> str:
    resp = await _post(
        client,
        _jsonrpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "peek-test", "version": "1.0.0"},
            },
        ),
    )
    assert resp.status_code == 200
    return resp.headers.get("mcp-session-id", "")


async def _call(
    client: httpx.AsyncClient,
    name: str,
    args: dict,
    sid: str = "",
) -> dict:
    body = _jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2)
    resp = await _post(client, body, sid)
    assert resp.status_code == 200
    return resp.json()["result"]


@pytest.mark.asyncio
async def test_peek_inbox_round_trip_mcp(mcp_peek: tuple) -> None:
    app, client, _backend = mcp_peek
    await app.state.backends.key_store.create_key("dev:alice")
    set_sender_label("dev:bob")
    sid = await _init(client)

    from .conftest import seed_inbox_bundle

    await seed_inbox_bundle(
        app.state.backends.backend,
        "dev:alice",
        "handoff",
        files=[("plan.md", b"# Plan"), ("config.yaml", b"key: value")],
        sender="dev:bob",
    )

    # Peek as bob (still the last sender)
    set_sender_label("dev:alice")
    peek = await _call(
        client,
        "cassetta_peek",
        {"path": "inbox/dev:alice/handoff"},
        sid,
    )
    assert peek.get("isError") is not True
    payload = json.loads(peek["content"][0]["text"])
    assert payload["bundle"]["sender"] == "dev:bob"
    assert payload["bundle"]["file_count"] == 2
    names = {f["name"] for f in payload["bundle"]["files"]}
    assert names == {"plan.md", "config.yaml"}

    # Subsequent inbox still lists the bundle (peek was non-destructive)
    listed = await _call(client, "cassetta_inbox", {"agent": "dev:alice"}, sid)
    listed_entries = json.loads(listed["content"][0]["text"])
    assert any(e["path"] == "handoff" for e in listed_entries)

    # Subsequent pick returns the original envelope
    set_sender_label("dev:alice")
    picked = await _call(client, "cassetta_pick", {"path": "handoff"}, sid)
    pick_body = json.loads(picked["content"][0]["text"])
    assert pick_body["bundle"]["file_count"] == 2
    pick_names = {f["name"] for f in pick_body["files"]}
    assert pick_names == {"plan.md", "config.yaml"}


@pytest.mark.asyncio
async def test_peek_store_via_bare_and_prefixed_paths(mcp_peek: tuple) -> None:
    _app, client, _backend = mcp_peek
    sid = await _init(client)

    await _call(
        client,
        "cassetta_put",
        {
            "path": "some/thing",
            "content": "hello",
        },
        sid,
    )

    bare = await _call(client, "cassetta_peek", {"path": "some/thing"}, sid)
    bare_body = json.loads(bare["content"][0]["text"])
    assert bare_body["bundle"]["file_count"] == 1

    prefixed = await _call(
        client,
        "cassetta_peek",
        {"path": "store/some/thing"},
        sid,
    )
    prefixed_body = json.loads(prefixed["content"][0]["text"])
    assert prefixed_body == bare_body

    listed = await _call(client, "cassetta_list", {}, sid)
    assert "some/thing" in listed["content"][0]["text"]


@pytest.mark.asyncio
async def test_peek_unknown_path_errors(mcp_peek: tuple) -> None:
    _app, client, _backend = mcp_peek
    sid = await _init(client)

    peek = await _call(client, "cassetta_peek", {"path": "ghost"}, sid)
    assert peek.get("isError") is True
    text = peek["content"][0]["text"]
    assert "Not found:" in text
