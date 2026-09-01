"""MCP ↔ REST parity for directed send-init phase-1.

One fixed, batch-requiring ``(to, path, manifest)`` is driven through both
``cassetta_send_init`` (MCP) and ``POST /uploads`` (REST). Both surfaces call
the shared ``prepare_send_init`` helper, so the minted batch tokens MUST agree
on the deterministic claims — ``bundle_path``, ``recipient``, ``sender``,
``mode``, ``manifest`` — and both MUST select batch with a byte-identical
``upload_url``.

Per-invocation claims are volatile by construction and are intentionally NOT
compared for equality: ``bundle_id`` (a fresh UUID), ``jti`` (injected per
sign), and the ``iat`` / ``nbf`` / ``exp`` timestamps. They are only checked
for presence. A divergence here would be the token randomness, not a behavior
difference between the two transports.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from cassetta.auth import jwt_tokens

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

# Same signing key the dev-mode ``client`` fixture boots with
# (``CASSETTA_JWT_KEY`` is base64; ``load_config`` decodes it to bytes).
_KEY = base64.b64decode("dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0")

# A bare recipient name resolves by passthrough (no key needed in dev mode);
# the single 200 KB file is larger than the default ``max_inline_size``
# (102400), so the MCP policy also selects batch — REST always forces batch.
_TO = "alice"
_PATH = "project-drop.tgz"
_MANIFEST = {"file_count": 1, "files": [{"name": "big.bin", "size": 200_000}]}

_DETERMINISTIC_CLAIMS = ("bundle_path", "recipient", "sender", "mode", "manifest")
_VOLATILE_CLAIMS = ("bundle_id", "jti", "iat", "nbf", "exp")


async def _mcp_send_init(client: httpx.AsyncClient) -> dict:
    """Drive ``cassetta_send_init`` over the MCP endpoint; return its result dict."""
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "parity-test", "version": "1.0.0"},
        },
    }
    resp = await client.post("/mcp/", json=init_msg, headers=MCP_HEADERS)
    assert resp.status_code == 200, resp.text
    sid = resp.headers.get("mcp-session-id", "")

    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "cassetta_send_init",
            "arguments": {"to": _TO, "path": _PATH, "manifest": _MANIFEST},
        },
    }
    headers = dict(MCP_HEADERS, **{"mcp-session-id": sid})
    resp = await client.post("/mcp/", json=call, headers=headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_send_init_mcp_rest_parity(client: httpx.AsyncClient) -> None:
    mcp_body = await _mcp_send_init(client)
    rest_resp = await client.post(
        "/uploads",
        json={"to": _TO, "path": _PATH, "manifest": _MANIFEST},
    )
    assert rest_resp.status_code == 201, rest_resp.text
    rest_body = rest_resp.json()

    # Both surfaces select batch with a byte-identical upload URL.
    assert mcp_body["mode"] == "batch"
    assert rest_body["mode"] == "batch"
    assert mcp_body["upload_url"] == rest_body["upload_url"]

    # The batch tokens verify against the signing key and carry identical
    # deterministic claims.
    mcp_claims = jwt_tokens.verify(mcp_body["batch_token"], primary=_KEY, secondary=None)
    rest_claims = jwt_tokens.verify(rest_body["batch_token"], primary=_KEY, secondary=None)
    assert {k: mcp_claims[k] for k in _DETERMINISTIC_CLAIMS} == {k: rest_claims[k] for k in _DETERMINISTIC_CLAIMS}

    # bundle_id is present and valid on both, and unique per invocation.
    assert mcp_body["bundle_id"] and rest_body["bundle_id"]
    assert mcp_body["bundle_id"] != rest_body["bundle_id"]

    # Volatile claims exist on both but are not compared for equality.
    for claims in (mcp_claims, rest_claims):
        for vol in _VOLATILE_CLAIMS:
            assert vol in claims
