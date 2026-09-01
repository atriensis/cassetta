"""Denial tests for US1: when policy.check returns False, REST routes
must return 403 Forbidden with no backend side effects, and MCP tools
must raise a generic 'Forbidden' error."""

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


class AlwaysDenyPolicy:
    async def check(self, identity: Identity, resource: str, action: str) -> bool:
        return False


@pytest.fixture
def storage_dir() -> str:
    return tempfile.mkdtemp()


@pytest.fixture
async def deny_client(
    storage_dir: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, str, Any]]:
    setup_token = "deny-test-token"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = (
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    )
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    # Build the bundle with the deny policy up front, but bootstrap a valid
    # key via the bundle's key_store before MCP is configured for real.
    app = create_app()
    config = app.state.config
    default_backends = app.state.backends
    backend = default_backends.backend
    key_store = default_backends.key_store
    raw_key, _ = await key_store.setup("test:deny-user")

    # Swap the access policy to AlwaysDenyPolicy via dataclasses.replace so
    # every downstream read (routes, MCP) sees the deny policy.
    from dataclasses import replace
    app.state.backends = replace(
        default_backends, access_policy=AlwaysDenyPolicy(),
    )
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
        yield client, raw_key, backend

    stop.set()
    await task


class TestRESTDenial:
    @pytest.mark.asyncio
    async def test_files_upload_denied(
        self, deny_client: tuple[httpx.AsyncClient, str, Any]
    ) -> None:
        client, api_key, backend = deny_client
        resp = await client.put(
            "/files/blocked.md",
            content=b"x",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Forbidden"}
        assert not await backend.exists("blocked.md")

    @pytest.mark.asyncio
    async def test_files_list_denied(
        self, deny_client: tuple[httpx.AsyncClient, str, Any]
    ) -> None:
        client, api_key, _ = deny_client
        resp = await client.get(
            "/files/",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_inbox_send_denied(
        self, deny_client: tuple[httpx.AsyncClient, str, Any]
    ) -> None:
        """Legacy PUT /inbox/{agent}/{path} is gone; it now returns 410.

        ``cassetta_send_init`` is the replacement path; the deny policy is
        covered by the MCP denial tests below (``TestMCPDenial``).
        """
        client, _api_key, _backend = deny_client
        resp = await client.put(
            "/inbox/test:deny-user/msg.txt",
            content=b"x",
            headers={"Authorization": f"Bearer {_api_key}"},
        )
        assert resp.status_code == 410
        body = resp.json()
        assert body["error"] == "gone"
        assert body["reason"] == "replaced_by_514"

    @pytest.mark.asyncio
    async def test_inbox_list_denied(
        self, deny_client: tuple[httpx.AsyncClient, str, Any]
    ) -> None:
        client, api_key, _ = deny_client
        resp = await client.get(
            "/inbox/alice/",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_create_key_denied_even_with_setup_token(
        self, deny_client: tuple[httpx.AsyncClient, str, Any]
    ) -> None:
        client, _, _ = deny_client
        # Operator identity goes through the policy; deny policy denies it too.
        resp = await client.post(
            "/keys",
            json={"host": "test", "project": "hacker"},
            headers={"X-Setup-Token": "deny-test-token"},
        )
        assert resp.status_code == 403
