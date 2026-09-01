"""Wiring tests for US1: every storage/inbox/keys/MCP call site must
call `policy.check(...)` with the expected resource and action."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp
from cassetta.protocols.identity import Identity

from .conftest import seed_inbox_bundle


class SpyAccessPolicy:
    """Records every (resource, action) and always allows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def check(self, identity: Identity, resource: str, action: str) -> bool:
        self.calls.append((resource, action))
        return True


@pytest.fixture
def storage_dir() -> str:
    return tempfile.mkdtemp()


@pytest.fixture
async def spy_auth_client(
    storage_dir: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]]:
    """Auth-mode app with a SpyAccessPolicy installed."""
    setup_token = "test-wiring-token"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    spy = SpyAccessPolicy()
    app = create_app()
    config = app.state.config
    from dataclasses import replace

    app.state.backends = replace(app.state.backends, access_policy=spy)
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
        yield client, setup_token, spy, backend

    stop.set()
    await task


async def _make_setup_key(client: httpx.AsyncClient, setup_token: str, label: str = "wire-user") -> str:
    resp = await client.post(
        "/setup",
        json={"host": "test", "project": label},
        headers={"X-Setup-Token": setup_token},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


class TestFilesRoutesCallPolicy:
    @pytest.mark.asyncio
    async def test_upload_calls_write(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token)
        spy.calls.clear()
        resp = await client.put(
            "/files/report.md",
            content=b"hello",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code in (200, 201)
        assert ("files:report.md", "write") in spy.calls

    @pytest.mark.asyncio
    async def test_download_calls_read(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token)
        await client.put(
            "/files/doc.md",
            content=b"x",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        spy.calls.clear()
        resp = await client.get(
            "/files/doc.md",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert ("files:doc.md", "read") in spy.calls

    @pytest.mark.asyncio
    async def test_list_calls_list(self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token)
        spy.calls.clear()
        resp = await client.get(
            "/files/",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert ("files:*", "list") in spy.calls

    @pytest.mark.asyncio
    async def test_delete_calls_delete(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token)
        await client.put(
            "/files/rm.md",
            content=b"x",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        spy.calls.clear()
        resp = await client.delete(
            "/files/rm.md",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert ("files:rm.md", "delete") in spy.calls


class TestInboxRoutesCallPolicy:
    @pytest.mark.asyncio
    async def test_list_inbox_calls_list(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token, "alice")
        spy.calls.clear()
        resp = await client.get(
            "/inbox/test:alice/",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert ("inbox:test:alice", "list") in spy.calls

    @pytest.mark.asyncio
    async def test_get_inbox_file_calls_read(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, backend = spy_auth_client
        api_key = await _make_setup_key(client, token, "alice")
        await seed_inbox_bundle(
            backend,
            "test:alice",
            "msg.txt",
            content=b"hi",
            sender="test:alice",
        )
        spy.calls.clear()
        resp = await client.get(
            "/inbox/test:alice/msg.txt",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert ("inbox:test:alice", "read") in spy.calls

    @pytest.mark.asyncio
    async def test_pick_calls_pick(self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]) -> None:
        client, token, spy, backend = spy_auth_client
        api_key = await _make_setup_key(client, token, "alice")
        await seed_inbox_bundle(
            backend,
            "test:alice",
            "m.txt",
            content=b"hi",
            sender="test:alice",
        )
        spy.calls.clear()
        resp = await client.post(
            "/inbox/test:alice/m.txt/pick",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert ("inbox:test:alice", "pick") in spy.calls

    @pytest.mark.asyncio
    async def test_delete_inbox_calls_delete(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, backend = spy_auth_client
        api_key = await _make_setup_key(client, token, "alice")
        await seed_inbox_bundle(
            backend,
            "test:alice",
            "x.txt",
            content=b"hi",
            sender="test:alice",
        )
        spy.calls.clear()
        resp = await client.delete(
            "/inbox/test:alice/x.txt",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 204
        assert ("inbox:test:alice", "delete") in spy.calls


class TestKeysRoutesCallPolicy:
    @pytest.mark.asyncio
    async def test_setup_calls_create(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        spy.calls.clear()
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "first"},
            headers={"X-Setup-Token": token},
        )
        assert resp.status_code == 201
        assert ("keys:test:first", "create") in spy.calls

    @pytest.mark.asyncio
    async def test_create_key_calls_create(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        await _make_setup_key(client, token)
        spy.calls.clear()
        resp = await client.post(
            "/keys",
            json={"host": "test", "project": "second"},
            headers={"X-Setup-Token": token},
        )
        assert resp.status_code == 201
        assert ("keys:test:second", "create") in spy.calls

    @pytest.mark.asyncio
    async def test_list_keys_calls_list(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        await _make_setup_key(client, token)
        spy.calls.clear()
        resp = await client.get(
            "/keys",
            headers={"X-Setup-Token": token},
        )
        assert resp.status_code == 200
        assert ("keys:*", "list") in spy.calls

    @pytest.mark.asyncio
    async def test_rotate_key_calls_rotate(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        await _make_setup_key(client, token, "r")
        spy.calls.clear()
        resp = await client.post(
            "/keys/test:r/rotate",
            headers={"X-Setup-Token": token},
        )
        assert resp.status_code == 200
        assert ("keys:test:r", "rotate") in spy.calls

    @pytest.mark.asyncio
    async def test_revoke_key_calls_revoke(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        await _make_setup_key(client, token, "v")
        spy.calls.clear()
        resp = await client.delete(
            "/keys/test:v",
            headers={"X-Setup-Token": token},
        )
        assert resp.status_code == 200
        assert ("keys:test:v", "revoke") in spy.calls


# ============================================================================
# MCP wiring tests — exercise each tool via the streamable HTTP adapter
# ============================================================================


MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


async def _post_mcp(client: httpx.AsyncClient, body: dict, sid: str, api_key: str) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    headers["Authorization"] = f"Bearer {api_key}"
    if sid:
        headers["mcp-session-id"] = sid
    return await client.post("/mcp/", json=body, headers=headers)


async def _init_mcp(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await _post_mcp(
        client,
        _jsonrpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "wiring-test", "version": "1.0.0"},
            },
        ),
        "",
        api_key,
    )
    assert resp.status_code == 200
    return resp.headers.get("mcp-session-id", "")


async def _call_mcp_tool(
    client: httpx.AsyncClient,
    name: str,
    args: dict,
    sid: str,
    api_key: str,
) -> httpx.Response:
    return await _post_mcp(
        client,
        _jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2),
        sid,
        api_key,
    )


class TestMCPToolsCallPolicy:
    @pytest.mark.asyncio
    async def test_put_calls_write(self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token)
        sid = await _init_mcp(client, api_key)
        spy.calls.clear()
        await _call_mcp_tool(client, "cassetta_put", {"path": "mcp.md", "content": "x"}, sid, api_key)
        assert ("files:mcp.md", "write") in spy.calls

    @pytest.mark.asyncio
    async def test_get_calls_read(self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token)
        sid = await _init_mcp(client, api_key)
        await _call_mcp_tool(client, "cassetta_put", {"path": "g.md", "content": "x"}, sid, api_key)
        spy.calls.clear()
        await _call_mcp_tool(client, "cassetta_get", {"path": "g.md"}, sid, api_key)
        assert ("files:g.md", "read") in spy.calls

    @pytest.mark.asyncio
    async def test_delete_calls_delete(
        self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]
    ) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token)
        sid = await _init_mcp(client, api_key)
        await _call_mcp_tool(client, "cassetta_put", {"path": "d.md", "content": "x"}, sid, api_key)
        spy.calls.clear()
        await _call_mcp_tool(client, "cassetta_delete", {"path": "d.md"}, sid, api_key)
        assert ("files:d.md", "delete") in spy.calls

    @pytest.mark.asyncio
    async def test_list_calls_list(self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token)
        sid = await _init_mcp(client, api_key)
        spy.calls.clear()
        await _call_mcp_tool(client, "cassetta_list", {}, sid, api_key)
        assert ("files:*", "list") in spy.calls

    @pytest.mark.asyncio
    async def test_send_calls_write(self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token, "wire-user")
        sid = await _init_mcp(client, api_key)
        # Create recipient key so alias resolution succeeds
        await client.post(
            "/keys",
            json={"host": "test", "project": "bob"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        spy.calls.clear()
        await _call_mcp_tool(
            client,
            "cassetta_send_init",
            {
                "path": "msg.txt",
                "to": "test:bob",
                "manifest": {
                    "file_count": 1,
                    "files": [{"name": "msg.txt", "size": 2}],
                },
            },
            sid,
            api_key,
        )
        assert ("inbox:test:bob", "write") in spy.calls

    @pytest.mark.asyncio
    async def test_inbox_calls_list(self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]) -> None:
        client, token, spy, _ = spy_auth_client
        api_key = await _make_setup_key(client, token, "wire-user")
        sid = await _init_mcp(client, api_key)
        spy.calls.clear()
        await _call_mcp_tool(client, "cassetta_inbox", {"agent": "wire-user"}, sid, api_key)
        assert ("inbox:wire-user", "list") in spy.calls

    @pytest.mark.asyncio
    async def test_pick_calls_pick(self, spy_auth_client: tuple[httpx.AsyncClient, str, SpyAccessPolicy, Any]) -> None:
        client, token, spy, backend = spy_auth_client
        api_key = await _make_setup_key(client, token, "wire-user")
        sid = await _init_mcp(client, api_key)
        # Seed so there's something to pick
        await seed_inbox_bundle(
            backend,
            "test:wire-user",
            "ready.txt",
            content=b"hi",
            sender="test:wire-user",
        )
        spy.calls.clear()
        await _call_mcp_tool(client, "cassetta_pick", {"path": "ready.txt"}, sid, api_key)
        assert ("inbox:test:wire-user", "pick") in spy.calls
