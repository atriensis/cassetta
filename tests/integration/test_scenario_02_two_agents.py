"""Integration test: two agents exchanging messages via inbox."""

from __future__ import annotations


async def test_scenario_02_two_agents(
    core_app: tuple,
    h,
) -> None:
    client, setup_token = core_app

    # 1. Setup agent A
    key_a = await h.setup_agent(client, host="agent-a", project="proj-a")

    # 2. Create agent B
    key_b = await h.create_key(client, key_a, host="agent-b", project="proj-b")

    # 3. A sends to B's inbox
    await h.send_inline(
        client,
        key_a,
        to="agent-b:proj-b",
        path="hello.txt",
        content=b"from A to B",
    )

    # 4. B picks the message
    resp = await client.post(
        "/inbox/agent-b:proj-b/hello.txt/pick",
        headers=h.auth(key_b),
    )
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "from A to B"

    # 5. B sends to A's inbox
    await h.send_inline(
        client,
        key_b,
        to="agent-a:proj-a",
        path="reply.txt",
        content=b"from B to A",
    )

    # 6. A picks the reply
    resp = await client.post(
        "/inbox/agent-a:proj-a/reply.txt/pick",
        headers=h.auth(key_a),
    )
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "from B to A"
