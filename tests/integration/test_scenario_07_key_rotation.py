"""Integration test: key rotation — old key revoked, new key works."""

from __future__ import annotations

SETUP_TOKEN = "integration-test-token"


async def test_scenario_07_key_rotation(
    core_app: tuple,
    h,
) -> None:
    client, setup_token = core_app

    # 1. Setup agent
    old_key = await h.setup_agent(client, host="rot", project="agent")
    old_headers = h.auth(old_key)

    # 2. Send a message to own inbox before rotation (Brief 514 flow)
    await h.send_inline(
        client,
        old_key,
        to="rot:agent",
        path="pre-rotate.txt",
        content=b"before rotation",
    )

    # 3. Rotate the key using setup token
    resp = await client.post(
        "/keys/rot:agent/rotate",
        headers=h.admin(),
    )
    assert resp.status_code == 200
    new_key = resp.json()["api_key"]
    new_headers = h.auth(new_key)

    # 4. Old key should be rejected
    resp = await client.get("/files/anything", headers=old_headers)
    assert resp.status_code == 401

    # 5. New key can upload
    resp = await client.put(
        "/files/post-rotate.txt",
        content=b"after rotation",
        headers=new_headers,
    )
    assert resp.status_code == 201

    # 6. New key can download
    resp = await client.get("/files/post-rotate.txt", headers=new_headers)
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "after rotation"

    # 7. Pre-rotation inbox messages accessible with new key
    resp = await client.post(
        "/inbox/rot:agent/pre-rotate.txt/pick",
        headers=new_headers,
    )
    assert resp.status_code == 200
    pick_envelope = resp.json()
    assert pick_envelope["mode"] == "inline"
    assert pick_envelope["files"][0]["content"] == "before rotation"
