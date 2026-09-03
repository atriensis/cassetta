"""Capabilities calls MUST NOT mutate storage.

Hash the storage directory (relative paths + sizes) before and after a
mixed batch of 100 MCP + REST capability calls; the hash must be
unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
                "clientInfo": {"name": "no-side-effects", "version": "1.0.0"},
            },
        ),
        headers=MCP_HEADERS,
    )
    assert resp.status_code == 200
    return resp.headers.get("mcp-session-id", "")


async def _call_mcp(client: httpx.AsyncClient, sid: str) -> dict:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    resp = await client.post(
        "/mcp/",
        json=_jsonrpc(
            "tools/call",
            {"name": "cassetta_capabilities", "arguments": {}},
            req_id=2,
        ),
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()["result"]


def _snapshot_dir(root: str) -> str:
    items: list[str] = []
    base = Path(root)
    if not base.exists():
        return ""
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if path.is_file():
            items.append(f"{rel}|{path.stat().st_size}")
        else:
            items.append(f"{rel}/")
    return hashlib.sha256("\n".join(items).encode()).hexdigest()


@pytest.mark.asyncio
async def test_100_capabilities_calls_leave_storage_untouched(
    client: httpx.AsyncClient,
    storage_dir: str,
) -> None:
    sid = await _init(client)

    before = _snapshot_dir(storage_dir)

    for i in range(50):
        # REST branch
        resp = await client.get("/capabilities")
        assert resp.status_code == 200
        # MCP branch
        result = await _call_mcp(client, sid)
        assert result.get("isError") is not True
        # Sanity: both parse to the same doc content.
        rest_doc = resp.json()
        mcp_doc = json.loads(result["content"][0]["text"])
        if i == 0:
            # Parity spot check once to avoid making this a parity test.
            assert rest_doc == mcp_doc

    after = _snapshot_dir(storage_dir)
    assert before == after
