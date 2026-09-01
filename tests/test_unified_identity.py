"""Tests for the unified get_current_identity dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp


async def _start_mcp(app):  # noqa: ANN001
    import asyncio

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


@pytest.fixture
async def identity_client(
    tmp_path,  # noqa: ANN001
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    import os

    setup_token = "test-identity-setup-token"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = str(tmp_path)
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    configure_mcp(config, backends)

    stop_mcp = await _start_mcp(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as c:
        yield c, setup_token

    await stop_mcp()


class TestUnifiedIdentity:
    @pytest.mark.asyncio
    async def test_setup_token_yields_operator_identity(self, identity_client: tuple[httpx.AsyncClient, str]) -> None:
        client, setup_token = identity_client
        # /setup accepts the setup token and should succeed (operator identity
        # passes the admin-level policy.check under DefaultAccessPolicy).
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "first-key"},
            headers={"X-Setup-Token": setup_token},
        )
        assert resp.status_code == 201
        assert resp.json()["label"] == "test:first-key"

    @pytest.mark.asyncio
    async def test_bearer_token_yields_user_identity(self, identity_client: tuple[httpx.AsyncClient, str]) -> None:
        client, setup_token = identity_client
        # Create a key first
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "bob"},
            headers={"X-Setup-Token": setup_token},
        )
        api_key = resp.json()["api_key"]

        # Now hit a protected route with bearer only — should succeed
        resp = await client.get(
            "/files/",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_missing_auth_returns_401(self, identity_client: tuple[httpx.AsyncClient, str]) -> None:
        client, _ = identity_client
        # No auth header on a protected route
        resp = await client.get("/files/")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_setup_token_precedes_bearer(self, identity_client: tuple[httpx.AsyncClient, str]) -> None:
        client, setup_token = identity_client
        # Create a bearer key
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "carol"},
            headers={"X-Setup-Token": setup_token},
        )
        api_key = resp.json()["api_key"]

        # Hit /keys with BOTH headers — setup-token wins and grants operator access
        resp = await client.get(
            "/keys",
            headers={
                "X-Setup-Token": setup_token,
                "Authorization": f"Bearer {api_key}",
            },
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_bearer_returns_401(self, identity_client: tuple[httpx.AsyncClient, str]) -> None:
        client, _ = identity_client
        resp = await client.get(
            "/files/",
            headers={"Authorization": "Bearer cst_nonsense"},
        )
        assert resp.status_code == 401


class TestDevModeIdentity:
    @pytest.mark.asyncio
    async def test_dev_mode_allows_unauthenticated(self, client: httpx.AsyncClient) -> None:
        # Dev-mode fixture (from conftest) sets CASSETTA_SETUP_TOKEN=""
        resp = await client.get("/files/")
        assert resp.status_code == 200
