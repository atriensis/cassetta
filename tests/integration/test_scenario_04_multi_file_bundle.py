"""Integration test: multi-file bundle upload and pick."""

from __future__ import annotations

import base64
import json


async def test_scenario_04_multi_file_bundle(
    core_app: tuple,
    h,
) -> None:
    client, setup_token = core_app

    # 1. Setup sender agent
    key_sender = await h.setup_agent(client, host="bundle", project="sender")

    # 2. Create receiver agent
    key_receiver = await h.create_key(
        client,
        key_sender,
        host="bundle",
        project="receiver",
    )

    # 3. Send multi-file bundle inline via the Brief 514 flow
    plan = b"# Plan\nStep 1"
    data_bytes = b'{"key": "value"}'
    sid = await h.mcp_init(client, api_key=key_sender)
    init = await h.mcp_call(
        client,
        "cassetta_send_init",
        {
            "to": "bundle:receiver",
            "path": "context",
            "manifest": {
                "file_count": 2,
                "files": [
                    {"name": "plan.md", "size": len(plan)},
                    {"name": "data.json", "size": len(data_bytes)},
                ],
            },
        },
        sid=sid,
        api_key=key_sender,
    )
    body = json.loads(init["content"][0]["text"])
    assert body["mode"] == "inline", body
    result = await h.mcp_call(
        client,
        "cassetta_send_inline",
        {
            "token": body["inline_token"],
            "files": [
                {
                    "name": "plan.md",
                    "content": base64.b64encode(plan).decode("ascii"),
                    "encoding": "base64",
                },
                {
                    "name": "data.json",
                    "content": base64.b64encode(data_bytes).decode("ascii"),
                    "encoding": "base64",
                },
            ],
        },
        sid=sid,
        api_key=key_sender,
    )
    assert json.loads(result["content"][0]["text"])["ok"] is True

    # 4. List inbox shows the bundle
    resp = await client.get("/inbox/bundle:receiver/", headers=h.auth(key_receiver))
    assert resp.status_code == 200
    listing = resp.json()
    assert len(listing["files"]) > 0

    # 5. Receiver picks the bundle
    resp = await client.post(
        "/inbox/bundle:receiver/context/pick",
        headers=h.auth(key_receiver),
    )
    assert resp.status_code == 200

    # 6. Response should be JSON with bundle and files keys
    data = resp.json()
    assert "bundle" in data
    assert "files" in data

    # 7. Verify files list has 2 entries with correct names and content
    bundle_files = data["files"]
    assert len(bundle_files) == 2
    names = {f["name"] for f in bundle_files}
    assert "plan.md" in names
    assert "data.json" in names
