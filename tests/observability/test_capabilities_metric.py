"""Capabilities counter coverage — Brief 533 FR-008."""

from __future__ import annotations


async def test_rest_capabilities_query_increments_via_rest(obs_client) -> None:
    client, metrics = obs_client
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    queries = metrics.find("cassetta.capabilities.queries", "increment")
    assert any(c.tags and c.tags.get("via") == "rest" for c in queries)


async def test_devmode_capabilities_still_increments(obs_client) -> None:
    """Edge Cases #93 — dev-mode (no-auth) still increments."""
    client, metrics = obs_client
    metrics.calls.clear()
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    queries = metrics.find("cassetta.capabilities.queries", "increment")
    assert len([c for c in queries if c.tags and c.tags.get("via") == "rest"]) == 1


async def test_mcp_capabilities_query_increments_via_mcp(obs_client) -> None:
    client, metrics = obs_client
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "obs-test", "version": "1.0.0"},
        },
    }
    init_resp = await client.post(
        "/mcp/",
        json=init_body,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    sid = init_resp.headers.get("mcp-session-id", "")
    metrics.calls.clear()
    call_body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "cassetta_capabilities", "arguments": {}},
    }
    resp = await client.post(
        "/mcp/",
        json=call_body,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "mcp-session-id": sid,
        },
    )
    assert resp.status_code == 200, resp.text
    queries = metrics.find("cassetta.capabilities.queries", "increment")
    assert any(c.tags and c.tags.get("via") == "mcp" for c in queries), [c.tags for c in queries]
