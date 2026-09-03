"""MCP ↔ REST parity for the capabilities surface.

One dev-mode server; query both surfaces with equivalent identities;
assert the parsed JSON documents are dict-equal.
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


async def _init(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/mcp/",
        json=_jsonrpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "parity-test", "version": "1.0.0"},
            },
        ),
        headers=MCP_HEADERS,
    )
    assert resp.status_code == 200
    return resp.headers.get("mcp-session-id", "")


async def _call_mcp(client: httpx.AsyncClient, name: str, sid: str) -> dict:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    resp = await client.post(
        "/mcp/",
        json=_jsonrpc(
            "tools/call",
            {"name": name, "arguments": {}},
            req_id=2,
        ),
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()["result"]


@pytest.mark.asyncio
async def test_mcp_and_rest_return_same_document(
    client: httpx.AsyncClient,
) -> None:
    sid = await _init(client)
    mcp_result = await _call_mcp(client, "cassetta_capabilities", sid)
    assert mcp_result.get("isError") is not True
    mcp_doc = json.loads(mcp_result["content"][0]["text"])

    rest_resp = await client.get("/capabilities")
    assert rest_resp.status_code == 200
    rest_doc = rest_resp.json()

    # Parsed dicts are equal across surfaces.
    assert mcp_doc == rest_doc
