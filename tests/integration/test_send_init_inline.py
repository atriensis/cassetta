"""Integration: `send_init` (inline) + `send_inline` round-trip.

End-to-end over MCP: setup identity → `cassetta_send_init` (inline
branch) → `cassetta_send_inline` with both `utf8` and `base64` payloads →
recipient lists inbox → recipient `cassetta_pick` returns byte-identical
content.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest


def _tool_result_text(result: dict) -> str:
    """Extract the tool's return-text from an MCP ``tools/call`` result."""
    content = result.get("content") or []
    assert content, f"expected non-empty content, got {result!r}"
    return str(content[0].get("text", ""))


def _tool_result_json(result: dict) -> dict:
    return json.loads(_tool_result_text(result))


@pytest.mark.asyncio
async def test_send_init_inline_full_roundtrip(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "projX")
    recipient_key = await h.create_key(client, sender_key, "alice", "main")

    # --- Sender: init the upload (inline branch) ----------------------------
    sender_sid = await h.mcp_init(client, api_key=sender_key)
    init = await h.mcp_call(
        client,
        "cassetta_send_init",
        {
            "to": "alice:main",
            "path": "notes.md",
            "manifest": {
                "file_count": 2,
                "files": [
                    {"name": "src/main.py", "size": 23},
                    {"name": "README.md", "size": 7},
                ],
            },
        },
        sid=sender_sid,
        api_key=sender_key,
    )
    body = _tool_result_json(init)
    assert body["mode"] == "inline"
    token = body["inline_token"]
    bundle_id = body["bundle_id"]
    assert bundle_id
    assert body["expires_at"].endswith("Z")

    # --- Sender: complete the inline upload --------------------------------
    utf8_body = "print('hello cassetta')"
    b64_body = base64.b64encode(b"hiallo\n").decode("ascii")
    send = await h.mcp_call(
        client,
        "cassetta_send_inline",
        {
            "token": token,
            "files": [
                {
                    "name": "src/main.py",
                    "content": utf8_body,
                    "encoding": "utf8",
                },
                {
                    "name": "README.md",
                    "content": b64_body,
                    "encoding": "base64",
                },
            ],
        },
        sid=sender_sid,
        api_key=sender_key,
    )
    send_body = _tool_result_json(send)
    assert send_body == {"bundle_id": bundle_id, "ok": True}

    # --- Recipient: list inbox ---------------------------------------------
    recipient_sid = await h.mcp_init(client, api_key=recipient_key)
    inbox = await h.mcp_call(
        client,
        "cassetta_inbox",
        {"agent": "alice:main"},
        sid=recipient_sid,
        api_key=recipient_key,
    )
    listing = _tool_result_json(inbox)
    assert isinstance(listing, list)
    assert len(listing) == 1
    entry = listing[0]
    assert entry["path"] == "notes.md"
    assert entry["bundle_id"] == bundle_id
    names = {f["name"] for f in entry["files"]}
    assert names == {"src/main.py", "README.md"}

    # --- Recipient: pick the bundle (atomic consume) -----------------------
    pick = await h.mcp_call(
        client,
        "cassetta_pick",
        {"path": "notes.md"},
        sid=recipient_sid,
        api_key=recipient_key,
    )
    envelope = _tool_result_json(pick)
    assert envelope["bundle"]["bundle_id"] == bundle_id
    files = {f["name"]: f["content"] for f in envelope["files"]}
    assert files["src/main.py"] == utf8_body
    assert files["README.md"] == "hiallo\n"
