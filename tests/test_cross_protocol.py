"""Tests for cross-protocol interoperability: MCP <-> REST for inbox.

Brief 514 removed the legacy ``PUT /inbox/{agent}/{path}`` REST endpoint
and the ``cassetta_send`` MCP tool. These tests now seed the inbox
directly via the backend (using :func:`seed_inbox_bundle`) and then
assert that REST and MCP retrieval paths still work end-to-end.
"""

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

from .conftest import seed_inbox_bundle

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


@pytest.fixture
def xproto_storage_dir() -> str:
    return tempfile.mkdtemp()


@pytest.fixture
async def xproto_app(
    xproto_storage_dir: str,
) -> AsyncIterator[tuple]:
    """App with auth enabled for cross-protocol tests.

    Returns (app, client, backend, alice_key, bob_key).
    """
    setup_token = "test-xproto"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = xproto_storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY_FILE", None)

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    backend = backends.backend
    key_store = backends.key_store
    configure_mcp(config, backends)

    alice_key, _ = await key_store.setup("test:alice")
    bob_key, _ = await key_store.create_key("test:bob")

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
        yield app, client, backend, alice_key, bob_key

    stop.set()
    await task


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


async def _mcp_post(
    client: httpx.AsyncClient, body: dict, auth_key: str, sid: str = "",
) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    headers["Authorization"] = f"Bearer {auth_key}"
    if sid:
        headers["mcp-session-id"] = sid
    return await client.post("/mcp/", json=body, headers=headers)


async def _mcp_init(client: httpx.AsyncClient, auth_key: str) -> str:
    resp = await _mcp_post(client, _jsonrpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0.0"},
    }), auth_key)
    assert resp.status_code == 200
    return resp.headers.get("mcp-session-id", "")


async def _mcp_call(
    client: httpx.AsyncClient, name: str, args: dict,
    auth_key: str, sid: str,
) -> dict:
    body = _jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2)
    resp = await _mcp_post(client, body, auth_key, sid)
    assert resp.status_code == 200
    return resp.json()["result"]


class TestMCPSendRESTRetrieve:
    @pytest.mark.asyncio
    async def test_seeded_inbox_rest_list_and_get(self, xproto_app: tuple) -> None:
        _app, client, backend, _alice_key, bob_key = xproto_app

        # Seed: alice sent a bundle to bob (bypassing the removed send path).
        await seed_inbox_bundle(
            backend, "test:bob", "report.txt",
            content=b"mcp content", sender="test:alice",
        )

        # Bob retrieves via REST listing
        resp = await client.get(
            "/inbox/test:bob/", headers={"Authorization": f"Bearer {bob_key}"},
        )
        assert resp.status_code == 200
        files = resp.json()["files"]
        assert len(files) == 1
        assert files[0]["path"] == "report.txt"
        assert files[0]["sender"] == "test:alice"

        # Bob gets file via REST
        resp = await client.get(
            "/inbox/test:bob/report.txt",
            headers={"Authorization": f"Bearer {bob_key}"},
        )
        assert resp.status_code == 200
        envelope = resp.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "mcp content"
        assert resp.headers.get("x-cassetta-sender") == "test:alice"


class TestRESTSendMCPPick:
    @pytest.mark.asyncio
    async def test_seeded_inbox_mcp_pick(self, xproto_app: tuple) -> None:
        _app, client, backend, _alice_key, bob_key = xproto_app

        # Seed: alice sent a bundle to bob.
        await seed_inbox_bundle(
            backend, "test:bob", "task.json",
            content=b'{"task": "from rest"}', sender="test:alice",
        )

        # Bob picks via MCP
        set_sender_label("test:bob")
        sid = await _mcp_init(client, bob_key)
        result = await _mcp_call(client, "cassetta_pick", {
            "path": "task.json",
        }, bob_key, sid)
        assert result.get("isError") is not True
        envelope = json.loads(result["content"][0]["text"])
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == '{"task": "from rest"}'

        # Verify deleted
        assert not await backend.exists("inbox/test:bob/task.json")


class TestMCPSendRESTPick:
    @pytest.mark.asyncio
    async def test_seeded_inbox_rest_pick(self, xproto_app: tuple) -> None:
        _app, client, backend, _alice_key, bob_key = xproto_app

        # Seed: alice sent a bundle to bob.
        await seed_inbox_bundle(
            backend, "test:bob", "data.csv",
            content=b"a,b,c", sender="test:alice",
        )

        # Bob picks via REST
        resp = await client.post(
            "/inbox/test:bob/data.csv/pick",
            headers={"Authorization": f"Bearer {bob_key}"},
        )
        assert resp.status_code == 200
        envelope = resp.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "a,b,c"
        assert resp.headers.get("x-cassetta-sender") == "test:alice"

        # Verify deleted
        assert not await backend.exists("inbox/test:bob/data.csv")
