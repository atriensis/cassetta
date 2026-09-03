"""Integration test: MCP tools — verify all MCP tool operations."""

from __future__ import annotations


async def test_scenario_06_mcp_tools(
    core_app: tuple,
    h,
) -> None:
    client, _ = core_app

    # 1. Setup agent
    api_key = await h.setup_agent(client, host="mcp-int", project="agent")

    # 2. Initialize MCP session (requires Bearer auth in non-dev mode)
    sid = await h.mcp_init(client, api_key=api_key)

    # 3. cassetta_put via MCP
    await h.mcp_call(
        client,
        "cassetta_put",
        {"path": "test.txt", "content": "mcp data"},
        sid,
        api_key=api_key,
    )

    # 4. Cross-protocol verify: GET via REST
    headers = h.auth(api_key)
    resp = await client.get("/files/test.txt", headers=headers)
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "mcp data"

    # 5. Send via the two-phase flow to own inbox
    await h.send_inline(
        client,
        api_key,
        to="mcp-int:agent",
        path="task.md",
        content=b"do this",
        sid=sid,
    )

    # 6. cassetta_pick via MCP
    pick_result = await h.mcp_call(
        client,
        "cassetta_pick",
        {"path": "task.md"},
        sid,
        api_key=api_key,
    )
    import json as _json

    pick_envelope = _json.loads(pick_result["content"][0]["text"])
    assert pick_envelope["mode"] == "inline"
    assert pick_envelope["files"][0]["content"] == "do this"

    # 7. cassetta_agents via MCP
    agents_result = await h.mcp_call(
        client,
        "cassetta_agents",
        {},
        sid,
        api_key=api_key,
    )
    agents_text = agents_result["content"][0]["text"]
    assert "mcp-int:agent" in agents_text

    # 8. Create second agent for broadcast test
    key2 = await h.create_key(client, api_key, host="mcp-int", project="agent2")

    # 9. cassetta_broadcast via MCP
    await h.mcp_call(
        client,
        "cassetta_broadcast",
        {"path": "announce.txt", "content": "hello all"},
        sid,
        api_key=api_key,
    )

    # 10. Second agent picks from inbox to verify delivery
    resp = await client.post(
        "/inbox/mcp-int:agent2/announce.txt/pick",
        headers=h.auth(key2),
    )
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "hello all"
