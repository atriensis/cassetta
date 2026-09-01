"""Tests for MCP tool multi-file bundle operations (Brief 509)."""

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


@pytest.fixture
def bundle_storage_dir() -> str:
    return tempfile.mkdtemp()


@pytest.fixture
def bundle_env(bundle_storage_dir: str) -> None:
    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = bundle_storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = (
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    )
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)


@pytest.fixture
async def mcp(bundle_env: None, bundle_storage_dir: str) -> AsyncIterator[tuple]:
    """Return (app, httpx client, backend) for MCP bundle testing."""
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


def _init_msg() -> dict:
    return _jsonrpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0.0"},
    })


async def _post(client: httpx.AsyncClient, body: dict, sid: str = "") -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    return await client.post("/mcp/", json=body, headers=headers)


async def _init(client: httpx.AsyncClient) -> str:
    resp = await _post(client, _init_msg())
    assert resp.status_code == 200
    return resp.headers.get("mcp-session-id", "")


async def _call(
    client: httpx.AsyncClient, name: str, args: dict, sid: str = "",
) -> dict:
    body = _jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2)
    resp = await _post(client, body, sid)
    assert resp.status_code == 200
    return resp.json()["result"]


# ============================================================
# T012: MCP pick bundle (bundles seeded directly — legacy send removed)
# ============================================================


class TestMcpPickBundle:

    @pytest.mark.asyncio
    async def test_pick_bundle_returns_json(self, mcp: tuple) -> None:
        """Pick multi-file bundle returns JSON with files list."""
        _app, client, backend = mcp
        await _app.state.backends.key_store.create_key("dev:agent")
        set_sender_label("dev:agent")
        sid = await _init(client)

        await seed_inbox_bundle(
            backend, "dev:agent", "my-bundle",
            files=[("plan.md", b"# Plan"), ("config.yaml", b"key: value")],
            sender="dev:agent",
        )

        result = await _call(client, "cassetta_pick", {"path": "my-bundle"}, sid)
        assert result.get("isError") is not True
        text = result["content"][0]["text"]
        data = json.loads(text)
        assert isinstance(data["bundle"], dict)
        assert data["bundle"]["schema_version"] == 1
        assert data["bundle"]["file_count"] == 2
        assert len(data["files"]) == 2
        assert data["files"][0]["name"] == "plan.md"
        assert data["files"][0]["content"] == "# Plan"
        assert data["files"][1]["name"] == "config.yaml"

    @pytest.mark.asyncio
    async def test_pick_single_file_unified_envelope(self, mcp: tuple) -> None:
        """Pick single-file returns unified inline envelope (Brief 515)."""
        _app, client, backend = mcp
        await _app.state.backends.key_store.create_key("dev:agent")
        set_sender_label("dev:agent")
        sid = await _init(client)

        await seed_inbox_bundle(
            backend, "dev:agent", "note.txt",
            content=b"hello", sender="dev:agent",
        )

        result = await _call(client, "cassetta_pick", {"path": "note.txt"}, sid)
        assert result.get("isError") is not True
        text = result["content"][0]["text"]
        envelope = json.loads(text)
        assert envelope["mode"] == "inline"
        assert isinstance(envelope["bundle"], dict)
        assert envelope["bundle"]["file_count"] == 1
        assert len(envelope["files"]) == 1
        assert envelope["files"][0]["name"] == "note.txt"
        assert envelope["files"][0]["content"] == "hello"
        assert envelope["files"][0]["encoding"] == "utf8"


# ============================================================
# T024-T025: MCP inbox listing with file_count (US3)
# ============================================================


class TestMcpInboxListing:

    @pytest.mark.asyncio
    async def test_inbox_listing_shows_file_count(self, mcp: tuple) -> None:
        """Inbox listing includes file_count for bundles and single files."""
        _app, client, backend = mcp
        await _app.state.backends.key_store.create_key("dev:agent")
        sid = await _init(client)

        # Seed a single file
        await seed_inbox_bundle(
            backend, "dev:agent", "note.txt",
            content=b"hello", sender="dev:peer",
        )

        # Seed a bundle
        await seed_inbox_bundle(
            backend, "dev:agent", "my-bundle",
            files=[("a.txt", b"aaa"), ("b.txt", b"bbb")],
            sender="dev:peer",
        )

        # List inbox
        result = await _call(client, "cassetta_inbox", {"agent": "dev:agent"}, sid)
        items = json.loads(result["content"][0]["text"])
        assert len(items) == 2

        # Find items by path
        by_path = {i["path"]: i for i in items}
        assert by_path["note.txt"]["file_count"] == 1
        assert by_path["my-bundle"]["file_count"] == 2


# ============================================================
# T029-T031: MCP put/get/list bundles in files namespace (US4)
# ============================================================


class TestMcpFilesBundles:

    @pytest.mark.asyncio
    async def test_put_bundle(self, mcp: tuple) -> None:
        """T029: cassetta_put with files parameter stores a bundle."""
        _app, client, backend = mcp
        sid = await _init(client)

        files_json = json.dumps([
            {"name": "src/main.py", "content": "print('hi')"},
            {"name": "README.md", "content": "# Readme"},
        ])
        result = await _call(client, "cassetta_put", {
            "path": "my-package", "files": files_json,
        }, sid)
        assert result.get("isError") is not True
        text = result["content"][0]["text"]
        assert "my-package" in text
        assert "2 files" in text

    @pytest.mark.asyncio
    async def test_get_bundle(self, mcp: tuple) -> None:
        """T030: cassetta_get for multi-file bundle returns JSON."""
        _app, client, backend = mcp
        sid = await _init(client)

        files_json = json.dumps([
            {"name": "plan.md", "content": "# Plan"},
            {"name": "code.py", "content": "x = 1"},
        ])
        await _call(client, "cassetta_put", {
            "path": "test-bundle", "files": files_json,
        }, sid)

        result = await _call(client, "cassetta_get", {"path": "test-bundle"}, sid)
        assert result.get("isError") is not True
        text = result["content"][0]["text"]
        data = json.loads(text)
        assert isinstance(data["bundle"], dict)
        assert data["bundle"]["schema_version"] == 1
        assert data["bundle"]["file_count"] == 2
        assert len(data["files"]) == 2
        assert data["files"][0]["name"] == "plan.md"

    @pytest.mark.asyncio
    async def test_list_shows_file_count(self, mcp: tuple) -> None:
        """T031: cassetta_list includes file_count for bundles."""
        _app, client, backend = mcp
        sid = await _init(client)

        # Store a single file
        await _call(client, "cassetta_put", {
            "path": "single.txt", "content": "solo",
        }, sid)

        # Store a bundle
        files_json = json.dumps([
            {"name": "a.txt", "content": "aaa"},
            {"name": "b.txt", "content": "bbb"},
        ])
        await _call(client, "cassetta_put", {
            "path": "multi-bundle", "files": files_json,
        }, sid)

        result = await _call(client, "cassetta_list", {}, sid)
        items = json.loads(result["content"][0]["text"])
        by_path = {i["path"]: i for i in items}
        assert by_path["single.txt"]["file_count"] == 1
        assert by_path["multi-bundle"]["file_count"] == 2


# ============================================================
# T041: MCP broadcast bundle (US5)
# ============================================================


class TestMcpBroadcastBundle:

    @pytest.mark.asyncio
    async def test_broadcast_bundle(self, mcp: tuple) -> None:
        """T041: cassetta_broadcast with files parameter sends to all agents."""
        _app, client, backend = mcp
        sid = await _init(client)

        # Register agents first

        key_store = _app.state.backends.key_store
        await key_store.create_key("alice:proj")
        await key_store.create_key("bob:proj")

        files_json = json.dumps([
            {"name": "update.md", "content": "# Update"},
            {"name": "data.csv", "content": "a,b,c"},
        ])
        result = await _call(client, "cassetta_broadcast", {
            "path": "team-update", "files": files_json,
        }, sid)
        assert result.get("isError") is not True
        text = result["content"][0]["text"]
        assert "2" in text  # 2 recipients

        # Verify both recipients have the bundle
        for label in ["alice:proj", "bob:proj"]:
            meta = await backend.read_bundle_meta(f"inbox/{label}/team-update")
            assert meta["file_count"] == 2
            assert len(meta["files"]) == 2
