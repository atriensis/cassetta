"""Integration test: send validation under the Brief 514 flow.

The legacy ``PUT /inbox/{agent}/{path}`` endpoint is removed — the test
now validates that it returns 410 with the structured body.
"""

from __future__ import annotations


async def test_scenario_05_send_validation(
    core_app: tuple,
    h,
) -> None:
    client, _ = core_app

    # 1. Setup agent
    key = await h.setup_agent(client, host="val", project="sender")
    headers = h.auth(key)

    # 2. Legacy PUT is gone — it returns 410 with the structured body.
    resp = await client.put(
        "/inbox/nonexistent:agent/msg.txt",
        content=b"should fail",
        headers=headers,
    )
    assert resp.status_code == 410, f"Expected 410 for legacy PUT send, got {resp.status_code}"
    body = resp.json()
    assert body == {
        "error": "gone",
        "reason": "replaced_by_two_phase_upload",
        "replacement": "POST /upload/{bundle_path}",
    }

    # 3. Verify nothing was stored — listing returns empty.
    resp2 = await client.get("/inbox/nonexistent:agent/", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["files"] == []
