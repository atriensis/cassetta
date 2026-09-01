"""Core integration test fixtures.

Provides a fully-wired Cassetta core app with real FilesystemBackend
and FileKeyStore on a temporary directory. Uses auth mode (setup token
required) since integration tests exercise the real auth path.

Helpers are exposed via the ``h`` fixture to avoid import conflicts
between core and cloud integration test directories.
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp
from cassetta.mime import pick_mime
from cassetta.protocols.storage import StorageBackend


async def seed_store_bundle(
    backend: StorageBackend,
    path: str,
    *,
    files: list[tuple[str, bytes]],
) -> str:
    """Populate a store bundle directly via the backend (bypasses policy caps).

    Used by Brief 515 tests that need an over-threshold bundle in the store
    namespace without going through `cassetta_put` (which would fail the
    `max_inline_size` check).
    """
    bundle_path = f"store/{path}"
    bundle_id = uuid.uuid4().hex
    writer = await backend.open_bundle_write(bundle_path)
    try:
        records: list[dict[str, Any]] = []
        for name, data in files:
            await writer.write_file(name, io.BytesIO(data))
            records.append({
                "name": name, "size": len(data),
                "mime": pick_mime(name, explicit=None),
            })
        meta: dict[str, Any] = {
            "schema_version": 1,
            "bundle_id": bundle_id,
            "sender": None,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": len(records),
            "files": records,
        }
        await writer.commit(meta)
        return bundle_id
    except Exception:
        await writer.abort()
        raise


async def seed_inbox_bundle(
    backend: StorageBackend,
    recipient: str,
    path: str,
    *,
    content: bytes | None = None,
    files: list[tuple[str, bytes]] | None = None,
    sender: str | None = None,
) -> str:
    """Populate an inbox bundle directly via the backend.

    Replaces the legacy PUT/MCP send path for test setup after Brief 514.
    Returns the bundle_id.
    """
    if (content is None) == (files is None):
        raise ValueError("pass exactly one of content or files")
    file_parts: list[tuple[str, bytes]] = (
        list(files) if files is not None else [(path.split("/")[-1], content or b"")]
    )
    bundle_path = f"inbox/{recipient}/{path}"
    bundle_id = uuid.uuid4().hex
    writer = await backend.open_bundle_write(bundle_path)
    try:
        records: list[dict[str, Any]] = []
        for name, data in file_parts:
            await writer.write_file(name, io.BytesIO(data))
            records.append({
                "name": name, "size": len(data),
                "mime": pick_mime(name, explicit=None),
            })
        meta: dict[str, Any] = {
            "schema_version": 1,
            "bundle_id": bundle_id,
            "sender": sender,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": len(records),
            "files": records,
        }
        await writer.commit(meta)
        return bundle_id
    except Exception:
        await writer.abort()
        raise

SETUP_TOKEN = "integration-test-token"

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _init_msg() -> dict:
    return _jsonrpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "integration-test", "version": "1.0.0"},
    })


@dataclass
class CoreHelpers:
    """Helper methods for core integration tests, injected via fixture."""

    setup_token: str = field(default=SETUP_TOKEN)

    @staticmethod
    def auth(api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    def admin() -> dict[str, str]:
        return {"X-Setup-Token": SETUP_TOKEN}

    @staticmethod
    async def setup_agent(
        client: httpx.AsyncClient, host: str, project: str,
    ) -> str:
        resp = await client.post(
            "/setup",
            json={"host": host, "project": project},
            headers={"X-Setup-Token": SETUP_TOKEN},
        )
        assert resp.status_code == 201, f"Setup failed: {resp.text}"
        return resp.json()["api_key"]

    @staticmethod
    async def create_key(
        client: httpx.AsyncClient, api_key: str, host: str, project: str,
    ) -> str:
        resp = await client.post(
            "/keys",
            json={"host": host, "project": project},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, f"Create key failed: {resp.text}"
        return resp.json()["api_key"]

    @staticmethod
    async def mcp_post(
        client: httpx.AsyncClient, body: dict, sid: str = "",
        api_key: str = "",
    ) -> httpx.Response:
        headers = dict(MCP_HEADERS)
        if sid:
            headers["mcp-session-id"] = sid
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return await client.post("/mcp/", json=body, headers=headers)

    @staticmethod
    async def mcp_init(client: httpx.AsyncClient, api_key: str = "") -> str:
        resp = await CoreHelpers.mcp_post(client, _init_msg(), api_key=api_key)
        assert resp.status_code == 200, f"MCP init failed: {resp.status_code} {resp.text}"
        return resp.headers.get("mcp-session-id", "")

    @staticmethod
    async def mcp_call(
        client: httpx.AsyncClient, name: str, args: dict, sid: str = "",
        api_key: str = "",
    ) -> dict:
        body = _jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2)
        resp = await CoreHelpers.mcp_post(client, body, sid, api_key=api_key)
        assert resp.status_code == 200, f"MCP call failed: {resp.status_code} {resp.text}"
        return resp.json()["result"]

    @staticmethod
    async def send_inline(
        client: httpx.AsyncClient, api_key: str, *,
        to: str, path: str, content: bytes, name: str | None = None,
        sid: str | None = None,
    ) -> str:
        """Brief 514 two-phase inline send helper.

        Returns the bundle_id. Replaces the removed ``PUT /inbox/{agent}/{path}``.
        """
        import base64
        import json as _json

        file_name = name or path
        if sid is None:
            sid = await CoreHelpers.mcp_init(client, api_key=api_key)
        init = await CoreHelpers.mcp_call(
            client, "cassetta_send_init",
            {
                "to": to, "path": path,
                "manifest": {
                    "file_count": 1,
                    "files": [{"name": file_name, "size": len(content)}],
                },
            },
            sid=sid, api_key=api_key,
        )
        body = _json.loads(init["content"][0]["text"])
        assert body.get("mode") == "inline", body
        result = await CoreHelpers.mcp_call(
            client, "cassetta_send_inline",
            {
                "token": body["inline_token"],
                "files": [{
                    "name": file_name,
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                }],
            },
            sid=sid, api_key=api_key,
        )
        done = _json.loads(result["content"][0]["text"])
        assert done.get("ok") is True, done
        return str(done["bundle_id"])


@pytest.fixture
def h() -> CoreHelpers:
    """Core integration test helpers."""
    return CoreHelpers()


async def _make_core_app(ttl: str = "0") -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    tmpdir = tempfile.mkdtemp()
    os.environ["CASSETTA_SETUP_TOKEN"] = SETUP_TOKEN
    os.environ["CASSETTA_STORAGE_PATH"] = tmpdir
    os.environ["CASSETTA_DEFAULT_TTL"] = ttl
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = (
        "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    )
    os.environ["CASSETTA_JWT_KEY"] = (
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    )
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY_FILE", None)
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    app = create_app()
    config = app.state.config
    backends = app.state.backends
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
        yield client, SETUP_TOKEN

    stop.set()
    await task


@pytest.fixture
async def core_app() -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Fully-wired core app. Yields (client, setup_token)."""
    async for item in _make_core_app(ttl="0"):
        yield item


@pytest.fixture
async def core_app_ttl() -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Core app with TTL=1 second for expiry tests."""
    async for item in _make_core_app(ttl="1"):
        yield item


@pytest.fixture
async def core_app_small_inline() -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Core app with ``max_inline_size=32`` so anything larger flows batch."""
    os.environ["CASSETTA_MAX_INLINE_SIZE"] = "32"
    try:
        async for item in _make_core_app(ttl="0"):
            yield item
    finally:
        os.environ.pop("CASSETTA_MAX_INLINE_SIZE", None)
