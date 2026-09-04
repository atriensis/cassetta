"""Effective single-use via TTL + bundle-path atomicity.

No server-side jti ledger; the "single-use" property is enforced
passively by the combination of TTL expiry, bundle-path commit atomicity
(second send targeting a committed path fails at bundle-writer open),
and request-path termination.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from cassetta.auth import jwt_tokens


def _unwrap(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


async def _call_raw(h, client, name, args, sid, api_key):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    return await h.mcp_post(client, body, sid=sid, api_key=api_key)


def _text(payload: dict) -> str:
    result = payload.get("result", {})
    content = result.get("content") or []
    if content:
        return str(content[0].get("text", ""))
    return json.dumps(result or payload.get("error") or {})


@pytest.mark.asyncio
async def test_success_then_reuse_hits_bundle_path_conflict(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender = await h.setup_agent(client, "bob", "u4-reuse")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    init = await h.mcp_call(
        client,
        "cassetta_send_init",
        {"to": "alice:main", "path": "n.md", "manifest": {"file_count": 1, "files": [{"name": "a.txt", "size": 3}]}},
        sid=sid,
        api_key=sender,
    )
    token = _unwrap(init)["inline_token"]
    ok = await h.mcp_call(
        client,
        "cassetta_send_inline",
        {"token": token, "files": [{"name": "a.txt", "content": "abc", "encoding": "utf8"}]},
        sid=sid,
        api_key=sender,
    )
    assert _unwrap(ok)["ok"] is True

    # Reuse the SAME token — bundle is already committed; expect bundle_path_conflict,
    # NOT an auth-layer rejection like "revoked".
    resp = await _call_raw(
        h,
        client,
        "cassetta_send_inline",
        {"token": token, "files": [{"name": "a.txt", "content": "abc", "encoding": "utf8"}]},
        sid,
        sender,
    )
    text = _text(resp.json())
    assert "bundle_path_conflict" in text
    assert "revoked" not in text


@pytest.mark.asyncio
async def test_expired_token_rejected(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """Forge an expired token via internal signing — we don't wait IRL."""
    client, _ = core_app
    app = client._transport.app  # type: ignore[attr-defined]
    sender = await h.setup_agent(client, "bob", "u4-exp")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    now = int(time.time())
    expired_claims = {
        "bundle_path": "inbox/alice:main/x.md",
        "bundle_id": "deadbeef",
        "sender": "bob:u4-exp",
        "recipient": "alice:main",
        "mode": "inline",
        "manifest": {"file_count": 1, "files": [{"name": "a.txt", "size": 3, "mime": None}]},
        "iat": now - 600,
        "nbf": now - 600,
        "exp": now - 60,
    }
    token = jwt_tokens.sign(expired_claims, key=app.state.config.jwt_primary_key)
    resp = await _call_raw(
        h,
        client,
        "cassetta_send_inline",
        {"token": token, "files": [{"name": "a.txt", "content": "abc", "encoding": "utf8"}]},
        sid,
        sender,
    )
    text = _text(resp.json())
    assert "unauthenticated" in text
    assert "expired" in text


@pytest.mark.asyncio
async def test_bad_signature_rejected(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender = await h.setup_agent(client, "bob", "u4-sig")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    # Sign with a totally different key.
    now = int(time.time())
    claims = {
        "bundle_path": "inbox/alice:main/x.md",
        "bundle_id": "cafebabe",
        "sender": "bob:u4-sig",
        "recipient": "alice:main",
        "mode": "inline",
        "manifest": {"file_count": 1, "files": [{"name": "a.txt", "size": 3, "mime": None}]},
        "iat": now,
        "nbf": now,
        "exp": now + 300,
    }
    token = jwt_tokens.sign(claims, key=b"\x01" * 32)

    resp = await _call_raw(
        h,
        client,
        "cassetta_send_inline",
        {"token": token, "files": [{"name": "a.txt", "content": "abc", "encoding": "utf8"}]},
        sid,
        sender,
    )
    text = _text(resp.json())
    assert "unauthenticated" in text
    assert "bad_signature" in text


@pytest.mark.asyncio
async def test_retry_after_rollback_may_succeed(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """Documented passive-single-use consequence: after an aborted attempt
    (no committed bundle), a retry under the same credential with a matching
    manifest MAY succeed because the target path is free again.
    """
    client, _ = core_app
    sender = await h.setup_agent(client, "bob", "u4-retry")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    init = await h.mcp_call(
        client,
        "cassetta_send_init",
        {"to": "alice:main", "path": "r.md", "manifest": {"file_count": 1, "files": [{"name": "a.txt", "size": 3}]}},
        sid=sid,
        api_key=sender,
    )
    token = _unwrap(init)["inline_token"]

    # First attempt fails mid-flight: wrong size → abort + rollback.
    resp1 = await _call_raw(
        h,
        client,
        "cassetta_send_inline",
        {"token": token, "files": [{"name": "a.txt", "content": "abcde", "encoding": "utf8"}]},
        sid,
        sender,
    )
    assert "manifest_violation" in _text(resp1.json())

    # Second attempt with matching payload succeeds — the path was freed.
    ok = await h.mcp_call(
        client,
        "cassetta_send_inline",
        {"token": token, "files": [{"name": "a.txt", "content": "abc", "encoding": "utf8"}]},
        sid=sid,
        api_key=sender,
    )
    assert _unwrap(ok)["ok"] is True
