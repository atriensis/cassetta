"""Integration test: TTL expiry — files and inbox messages expire."""

from __future__ import annotations

import time


async def test_scenario_08_ttl_expiry(
    core_app_ttl: tuple,
    h,
) -> None:
    client, setup_token = core_app_ttl

    # 1. Setup agent
    key = await h.setup_agent(client, host="ttl", project="agent")
    headers = h.auth(key)

    # 2. Upload a file
    resp = await client.put(
        "/files/ephemeral.txt",
        content=b"temporary",
        headers=headers,
    )
    assert resp.status_code == 201

    # 3. Wait for TTL expiry (TTL=1 second)
    time.sleep(2)

    # 4. File should be gone
    resp = await client.get("/files/ephemeral.txt", headers=headers)
    assert resp.status_code == 404

    # 5. Send an inbox message
    await h.send_inline(
        client,
        key,
        to="ttl:agent",
        path="temp-msg.txt",
        content=b"temp inbox",
    )

    # 6. Wait for TTL expiry
    time.sleep(2)

    # 7. Inbox should be empty (or the file gone)
    resp = await client.get("/inbox/ttl:agent/", headers=headers)
    assert resp.status_code == 200
    files = resp.json().get("files", [])
    names = [f["name"] for f in files]
    assert "temp-msg.txt" not in names
