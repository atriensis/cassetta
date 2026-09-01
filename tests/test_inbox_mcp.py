"""Tests for MCP inbox tools (cassetta_inbox, cassetta_pick)."""

import asyncio
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp
from cassetta.mcp_server import set_sender_label

from .conftest import seed_inbox_bundle

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
def mcp_inbox_storage_dir() -> str:
    return tempfile.mkdtemp()


@pytest.fixture
async def mcp_inbox_app(
    mcp_inbox_storage_dir: str,
) -> AsyncIterator[tuple]:
    """Dev mode MCP app with sender label set to 'alice'."""
    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = mcp_inbox_storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = (
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    )
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    backend = backends.backend
    configure_mcp(config, backends)

    # Register keys so alias resolver can resolve labels
    await backends.key_store.setup("test:alice")
    await backends.key_store.create_key("test:bob")

    # Set sender label (simulates authenticated user)
    set_sender_label("test:alice")

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


async def _post(client: httpx.AsyncClient, body: dict, sid: str = "") -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    return await client.post("/mcp/", json=body, headers=headers)


async def _init(client: httpx.AsyncClient) -> str:
    resp = await _post(client, _jsonrpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0.0"},
    }))
    assert resp.status_code == 200
    return resp.headers.get("mcp-session-id", "")


async def _call(client: httpx.AsyncClient, name: str, args: dict, sid: str) -> dict:
    body = _jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2)
    resp = await _post(client, body, sid)
    assert resp.status_code == 200
    return resp.json()["result"]


class TestCassettaInbox:
    @pytest.mark.asyncio
    async def test_inbox_lists_files(self, mcp_inbox_app: tuple) -> None:
        _app, client, backend = mcp_inbox_app
        sid = await _init(client)

        # Populate alice's inbox (self-send)
        await seed_inbox_bundle(
            backend, "test:alice", "msg1.txt",
            content=b"one", sender="test:alice",
        )
        time.sleep(0.05)
        await seed_inbox_bundle(
            backend, "test:alice", "msg2.txt",
            content=b"two", sender="test:alice",
        )

        result = await _call(client, "cassetta_inbox", {}, sid)
        assert result.get("isError") is not True
        files = json.loads(result["content"][0]["text"])
        assert len(files) == 2
        # Newest first
        assert files[0]["path"] == "msg2.txt"
        assert files[1]["path"] == "msg1.txt"

    @pytest.mark.asyncio
    async def test_inbox_lists_other_agent(self, mcp_inbox_app: tuple) -> None:
        _app, client, backend = mcp_inbox_app
        sid = await _init(client)

        await seed_inbox_bundle(
            backend, "test:bob", "for-bob.txt",
            content=b"hi", sender="test:alice",
        )

        result = await _call(client, "cassetta_inbox", {"agent": "test:bob"}, sid)
        files = json.loads(result["content"][0]["text"])
        assert len(files) == 1
        assert files[0]["path"] == "for-bob.txt"

    @pytest.mark.asyncio
    async def test_inbox_empty_returns_empty_list(self, mcp_inbox_app: tuple) -> None:
        _app, client, _backend = mcp_inbox_app
        sid = await _init(client)

        result = await _call(client, "cassetta_inbox", {}, sid)
        files = json.loads(result["content"][0]["text"])
        assert files == []


class TestCassettaPick:
    @pytest.mark.asyncio
    async def test_pick_returns_content_and_deletes(self, mcp_inbox_app: tuple) -> None:
        _app, client, backend = mcp_inbox_app
        sid = await _init(client)

        await seed_inbox_bundle(
            backend, "test:alice", "task.txt",
            content=b"do this", sender="test:alice",
        )

        result = await _call(client, "cassetta_pick", {"path": "task.txt"}, sid)
        assert result.get("isError") is not True
        envelope = json.loads(result["content"][0]["text"])
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "do this"

        with pytest.raises(FileNotFoundError):
            await backend.read_bundle_meta("inbox/test:alice/task.txt")

    @pytest.mark.asyncio
    async def test_pick_latest(self, mcp_inbox_app: tuple) -> None:
        _app, client, backend = mcp_inbox_app
        sid = await _init(client)

        await seed_inbox_bundle(
            backend, "test:alice", "old.txt",
            content=b"old msg", sender="test:alice",
        )
        time.sleep(0.05)
        await seed_inbox_bundle(
            backend, "test:alice", "new.txt",
            content=b"latest msg", sender="test:alice",
        )

        result = await _call(client, "cassetta_pick", {"path": "latest"}, sid)
        envelope = json.loads(result["content"][0]["text"])
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "latest msg"

    @pytest.mark.asyncio
    async def test_pick_missing_returns_error(self, mcp_inbox_app: tuple) -> None:
        _app, client, _backend = mcp_inbox_app
        sid = await _init(client)

        result = await _call(client, "cassetta_pick", {"path": "nope.txt"}, sid)
        assert result["isError"] is True

    @pytest.mark.asyncio
    async def test_pick_empty_inbox_latest_returns_error(
        self, mcp_inbox_app: tuple
    ) -> None:
        _app, client, _backend = mcp_inbox_app
        sid = await _init(client)

        result = await _call(client, "cassetta_pick", {"path": "latest"}, sid)
        assert result["isError"] is True
