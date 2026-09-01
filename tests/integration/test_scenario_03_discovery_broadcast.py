"""Integration test: agent discovery and broadcast messaging."""

from __future__ import annotations


async def test_scenario_03_discovery_broadcast(
    core_app: tuple,
    h,
) -> None:
    client, setup_token = core_app

    # 1. Setup agent alpha
    key_alpha = await h.setup_agent(client, host="disc", project="alpha")

    # 2. Create agents beta and gamma
    key_beta = await h.create_key(client, key_alpha, host="disc", project="beta")
    key_gamma = await h.create_key(client, key_alpha, host="disc", project="gamma")

    # 3. Discover all agents
    resp = await client.get("/agents", headers=h.auth(key_alpha))
    assert resp.status_code == 200
    data = resp.json()
    agents = data["agents"]
    assert len(agents) == 3

    # 4. Verify all labels present
    labels = {a["label"] for a in agents}
    assert labels == {"disc:alpha", "disc:beta", "disc:gamma"}

    # 5. Broadcast from alpha
    resp = await client.post(
        "/broadcast/news.txt",
        content=b"broadcast msg",
        headers=h.auth(key_alpha),
    )
    assert resp.status_code == 200
    bcast = resp.json()

    # 6. Alpha should not receive its own broadcast
    assert bcast["total_delivered"] == 2
    assert "disc:alpha" not in bcast["delivered_to"]

    # 7. Beta picks broadcast
    resp = await client.post(
        "/inbox/disc:beta/news.txt/pick",
        headers=h.auth(key_beta),
    )
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "broadcast msg"

    # 8. Gamma picks broadcast
    resp = await client.post(
        "/inbox/disc:gamma/news.txt/pick",
        headers=h.auth(key_gamma),
    )
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "broadcast msg"
