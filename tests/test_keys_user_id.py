"""Tests for BUG-3: POST /keys with optional user_id parameter."""

from __future__ import annotations

import httpx
import pytest


class TestKeysUserIdParam:
    @pytest.mark.asyncio
    async def test_create_key_with_user_id_accepted(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        """POST /keys with user_id should be accepted (no validation error)."""
        client, token = auth_client
        # Setup first key (required before POST /keys works)
        await client.post(
            "/setup",
            json={"host": "setup", "project": "first"},
            headers={"X-Setup-Token": token},
        )
        resp = await client.post(
            "/keys",
            json={"host": "ws1", "project": "proj1", "user_id": "user-123"},
            headers={"X-Setup-Token": token},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["label"] == "ws1:proj1"
        assert body["api_key"].startswith("cst_")

    @pytest.mark.asyncio
    async def test_create_key_without_user_id_backward_compat(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        """POST /keys without user_id should work as before."""
        client, token = auth_client
        await client.post(
            "/setup",
            json={"host": "setup", "project": "first"},
            headers={"X-Setup-Token": token},
        )
        resp = await client.post(
            "/keys",
            json={"host": "ws2", "project": "proj2"},
            headers={"X-Setup-Token": token},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["label"] == "ws2:proj2"

    @pytest.mark.asyncio
    async def test_create_key_user_id_null_backward_compat(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        """POST /keys with user_id=null should work as without user_id."""
        client, token = auth_client
        await client.post(
            "/setup",
            json={"host": "setup", "project": "first"},
            headers={"X-Setup-Token": token},
        )
        resp = await client.post(
            "/keys",
            json={"host": "ws3", "project": "proj3", "user_id": None},
            headers={"X-Setup-Token": token},
        )
        assert resp.status_code == 201, resp.text
