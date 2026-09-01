"""Tests for recipient validation on send (Brief 508).

Brief 514 removed the legacy ``PUT /inbox/{agent}/{path}``; these tests
now drive validation through ``cassetta_send_init``.
"""

from __future__ import annotations

import json

import httpx
import pytest


async def _mcp_post(
    client: httpx.AsyncClient, body: dict, sid: str = "", api_key: str = "",
) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if sid:
        headers["mcp-session-id"] = sid
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return await client.post("/mcp/", json=body, headers=headers)


async def _mcp_init(client: httpx.AsyncClient, api_key: str) -> str:
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "send-validation", "version": "1.0.0"},
        },
    }
    resp = await _mcp_post(client, body, api_key=api_key)
    assert resp.status_code == 200, resp.text
    return resp.headers.get("mcp-session-id", "")


async def _send_init(
    client: httpx.AsyncClient, sid: str, api_key: str,
    to: str, path: str = "hello.txt",
) -> dict:
    body = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "cassetta_send_init",
            "arguments": {
                "to": to, "path": path,
                "manifest": {
                    "file_count": 1,
                    "files": [{"name": path, "size": 5}],
                },
            },
        },
    }
    resp = await _mcp_post(client, body, sid=sid, api_key=api_key)
    assert resp.status_code == 200
    return resp.json()["result"]


class TestSendInitValidation:
    """``cassetta_send_init`` validates recipients via the alias resolver."""

    @pytest.mark.asyncio
    async def test_send_to_valid_label(
        self, auth_client: tuple[httpx.AsyncClient, str],
    ) -> None:
        client, token = auth_client
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "sender"},
            headers={"X-Setup-Token": token},
        )
        sender_key = resp.json()["api_key"]
        resp = await client.post(
            "/keys",
            json={"host": "test", "project": "receiver"},
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 201

        sid = await _mcp_init(client, sender_key)
        result = await _send_init(client, sid, sender_key, to="test:receiver")
        assert result.get("isError") is not True
        payload = json.loads(result["content"][0]["text"])
        assert "bundle_id" in payload

    @pytest.mark.asyncio
    async def test_send_to_unknown_label_rejected(
        self, auth_client: tuple[httpx.AsyncClient, str],
    ) -> None:
        client, token = auth_client
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "sender"},
            headers={"X-Setup-Token": token},
        )
        sender_key = resp.json()["api_key"]

        sid = await _mcp_init(client, sender_key)
        result = await _send_init(client, sid, sender_key, to="test:nonexistent")
        # Brief 521 FR-002: a label-style ``to`` that the resolver could
        # not match raises ``unknown_recipient`` instead of falling through
        # to a silent direct-recipient passthrough. The bundle is rejected
        # at send_init time, before any orphan inbox gets created.
        assert result.get("isError") is True
        assert "unknown_recipient" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_send_to_alias_without_colon_accepted_in_core(
        self, auth_client: tuple[httpx.AsyncClient, str],
    ) -> None:
        client, token = auth_client
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "sender"},
            headers={"X-Setup-Token": token},
        )
        sender_key = resp.json()["api_key"]

        sid = await _mcp_init(client, sender_key)
        result = await _send_init(client, sid, sender_key, to="bob")
        # Bare name (no colon) is treated as a direct recipient in core.
        assert result.get("isError") is not True
