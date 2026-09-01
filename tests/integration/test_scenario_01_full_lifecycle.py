"""Integration test: full file lifecycle (upload, download, inbox, pick)."""

from __future__ import annotations

import base64
import json


async def test_scenario_01_full_lifecycle(
    core_app: tuple,
    h,
) -> None:
    client, setup_token = core_app

    # 1. Setup agent
    api_key = await h.setup_agent(client, host="int-test", project="lifecycle")

    headers = h.auth(api_key)
    label = "int-test:lifecycle"

    # 2. Upload a file
    resp = await client.put("/files/doc.md", content=b"# Hello World", headers=headers)
    assert resp.status_code == 201

    # 3. Download the file
    resp = await client.get("/files/doc.md", headers=headers)
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "# Hello World"

    # 4. Send to own inbox via the new two-phase flow
    sid = await h.mcp_init(client, api_key=api_key)
    init = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": label, "path": "msg.md",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "msg.md", "size": len(b"inbox message")}],
            },
        },
        sid=sid, api_key=api_key,
    )
    body = json.loads(init["content"][0]["text"])
    assert body["mode"] == "inline"
    await h.mcp_call(
        client, "cassetta_send_inline",
        {
            "token": body["inline_token"],
            "files": [{
                "name": "msg.md",
                "content": base64.b64encode(b"inbox message").decode("ascii"),
                "encoding": "base64",
            }],
        },
        sid=sid, api_key=api_key,
    )

    # 5. List inbox
    resp = await client.get(f"/inbox/{label}/", headers=headers)
    assert resp.status_code == 200
    listing = resp.json()
    assert "msg.md" in [f["path"] for f in listing["files"]]

    # 6. Pick the message
    resp = await client.post(f"/inbox/{label}/msg.md/pick", headers=headers)
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "inbox message"

    # 7. Inbox is now empty
    resp = await client.get(f"/inbox/{label}/", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["files"] == []
