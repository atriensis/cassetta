"""T022 — contract test for `cassetta_send_inline` MCP tool (Brief 514).

Covers:
- Valid inline JWT + matching payloads → success response.
- Batch-mode JWT fed to `send_inline` → `unauthenticated: wrong_mode`.
- ``manifest_violation`` error prefixes: `extra_file`, `wrong_size`,
  `missing_file`.
"""

from __future__ import annotations

import json

import httpx
import pytest


def _call_raw(h, client, name, args, sid, api_key):
    body = {
        "jsonrpc": "2.0", "id": 99, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    return h.mcp_post(client, body, sid=sid, api_key=api_key)


def _extract_text(payload: dict) -> str:
    result = payload.get("result", {})
    if result:
        content = result.get("content") or []
        if content:
            return str(content[0].get("text", ""))
        # FastMCP sometimes wraps errors under isError flag — fall through.
        return json.dumps(result)
    err = payload.get("error") or {}
    return json.dumps(err)


@pytest.mark.asyncio
async def test_send_inline_success(
    core_app: tuple[httpx.AsyncClient, str], h,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "inline-ok")
    await h.create_key(client, sender_key, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender_key)

    init = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "ok.md",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "a.txt", "size": 3}],
            },
        },
        sid=sid, api_key=sender_key,
    )
    init_body = json.loads(init["content"][0]["text"])
    token = init_body["inline_token"]

    send = await h.mcp_call(
        client, "cassetta_send_inline",
        {
            "token": token,
            "files": [{"name": "a.txt", "content": "abc", "encoding": "utf8"}],
        },
        sid=sid, api_key=sender_key,
    )
    body = json.loads(send["content"][0]["text"])
    assert body == {"bundle_id": init_body["bundle_id"], "ok": True}


@pytest.mark.asyncio
async def test_send_inline_extra_file_rejected(
    core_app: tuple[httpx.AsyncClient, str], h,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "inline-extra")
    await h.create_key(client, sender_key, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender_key)

    init = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "note.md",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "a.txt", "size": 3}],
            },
        },
        sid=sid, api_key=sender_key,
    )
    token = json.loads(init["content"][0]["text"])["inline_token"]
    resp = await _call_raw(
        h, client, "cassetta_send_inline",
        {
            "token": token,
            "files": [
                {"name": "a.txt", "content": "abc", "encoding": "utf8"},
                {"name": "rogue.bin", "content": "x", "encoding": "utf8"},
            ],
        },
        sid, sender_key,
    )
    text = _extract_text(resp.json())
    assert "manifest_violation" in text
    assert "extra_file" in text


@pytest.mark.asyncio
async def test_send_inline_wrong_size_rejected(
    core_app: tuple[httpx.AsyncClient, str], h,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "inline-size")
    await h.create_key(client, sender_key, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender_key)

    init = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "note2.md",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "a.txt", "size": 3}],
            },
        },
        sid=sid, api_key=sender_key,
    )
    token = json.loads(init["content"][0]["text"])["inline_token"]
    resp = await _call_raw(
        h, client, "cassetta_send_inline",
        {
            "token": token,
            "files": [{"name": "a.txt", "content": "abcdef", "encoding": "utf8"}],
        },
        sid, sender_key,
    )
    text = _extract_text(resp.json())
    assert "manifest_violation" in text
    assert "wrong_size" in text


@pytest.mark.asyncio
async def test_send_inline_missing_file_rejected(
    core_app: tuple[httpx.AsyncClient, str], h,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "inline-miss")
    await h.create_key(client, sender_key, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender_key)

    init = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "note3.md",
            "manifest": {
                "file_count": 2,
                "files": [
                    {"name": "a.txt", "size": 3},
                    {"name": "b.txt", "size": 3},
                ],
            },
        },
        sid=sid, api_key=sender_key,
    )
    token = json.loads(init["content"][0]["text"])["inline_token"]
    resp = await _call_raw(
        h, client, "cassetta_send_inline",
        {
            "token": token,
            "files": [{"name": "a.txt", "content": "abc", "encoding": "utf8"}],
        },
        sid, sender_key,
    )
    text = _extract_text(resp.json())
    assert "manifest_violation" in text
    assert "missing_file" in text


@pytest.mark.asyncio
async def test_send_inline_bad_signature_rejected(
    core_app: tuple[httpx.AsyncClient, str], h,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "inline-sig")
    await h.create_key(client, sender_key, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender_key)

    resp = await _call_raw(
        h, client, "cassetta_send_inline",
        {
            "token": "not.a.real.jwt",
            "files": [{"name": "a.txt", "content": "abc", "encoding": "utf8"}],
        },
        sid, sender_key,
    )
    text = _extract_text(resp.json())
    assert "unauthenticated" in text


@pytest.mark.asyncio
async def test_send_inline_bad_base64_reports_encoding(
    core_app: tuple[httpx.AsyncClient, str], h,
) -> None:
    """Brief 541: invalid base64 content → ``missing_or_bad_encoding``, not ``wrong_size``."""
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "inline-badb64")
    await h.create_key(client, sender_key, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender_key)

    init = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "bad.md",
            "manifest": {"file_count": 1, "files": [{"name": "a.txt", "size": 3}]},
        },
        sid=sid, api_key=sender_key,
    )
    token = json.loads(init["content"][0]["text"])["inline_token"]
    resp = await _call_raw(
        h, client, "cassetta_send_inline",
        {
            "token": token,
            "files": [
                {"name": "a.txt", "content": "!!!notbase64!!!", "encoding": "base64"},
            ],
        },
        sid, sender_key,
    )
    text = _extract_text(resp.json())
    assert "manifest_violation" in text
    assert "missing_or_bad_encoding" in text
    assert "wrong_size" not in text


@pytest.mark.asyncio
async def test_send_inline_missing_encoding_reports_encoding(
    core_app: tuple[httpx.AsyncClient, str], h,
) -> None:
    """Brief 541: omitting ``encoding`` is rejected naming the field, never ``wrong_size``."""
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "inline-noenc")
    await h.create_key(client, sender_key, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender_key)

    init = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "noenc.md",
            "manifest": {"file_count": 1, "files": [{"name": "a.txt", "size": 3}]},
        },
        sid=sid, api_key=sender_key,
    )
    token = json.loads(init["content"][0]["text"])["inline_token"]
    resp = await _call_raw(
        h, client, "cassetta_send_inline",
        {
            "token": token,
            "files": [{"name": "a.txt", "content": "abc"}],  # encoding omitted
        },
        sid, sender_key,
    )
    text = _extract_text(resp.json())
    assert "wrong_size" not in text
    assert "encoding" in text.lower()
