"""T048 — US8: the legacy send path is gone.

Covers:
(a) MCP tool enumeration: ``cassetta_send`` is absent; both
    ``cassetta_send_init`` and ``cassetta_send_inline`` are registered.
(b) Legacy REST URL ``PUT /inbox/{agent}/{path}`` returns 410 Gone with
    a structured body naming the route that replaces it.
"""

from __future__ import annotations

import json

import httpx
import pytest

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


@pytest.mark.asyncio
async def test_legacy_mcp_send_removed(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """MCP tool enumeration confirms ``cassetta_send`` is gone."""
    client, _ = core_app
    api_key = await h.setup_agent(client, "u8", "tool-list")
    sid = await h.mcp_init(client, api_key=api_key)
    headers = dict(MCP_HEADERS)
    headers["Authorization"] = f"Bearer {api_key}"
    headers["mcp-session-id"] = sid
    resp = await client.post(
        "/mcp/",
        json=_jsonrpc("tools/list", {}, req_id=2),
        headers=headers,
    )
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "cassetta_send" not in names
    assert "cassetta_send_init" in names
    assert "cassetta_send_inline" in names


@pytest.mark.asyncio
async def test_legacy_rest_send_returns_410(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """``PUT /inbox/{agent}/{path}`` returns 410 with the structured body."""
    client, _ = core_app
    api_key = await h.setup_agent(client, "u8", "rest-legacy")
    resp = await client.put(
        "/inbox/alice/some-path",
        content=b"legacy body",
        headers=h.auth(api_key),
    )
    assert resp.status_code == 410
    body = resp.json()
    # Asserted whole, not field by field: a field added here reaches every client holding an
    # old URL, so it should have to be written down in two places.
    assert body == {
        "error": "gone",
        "reason": "replaced_by_two_phase_upload",
        "replacement": "POST /upload/{bundle_path}",
    }


# Silence pyright on unused json import when this file is collected alone.
_ = json
