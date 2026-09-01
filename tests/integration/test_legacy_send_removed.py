"""T048 — US8: the legacy send path is gone.

Covers:
(a) MCP tool enumeration: ``cassetta_send`` is absent; both
    ``cassetta_send_init`` and ``cassetta_send_inline`` are registered.
(b) Legacy REST URL ``PUT /inbox/{agent}/{path}`` returns 410 Gone with
    the structured body specified by the ``legacySendRemoved`` shape in
    ``contracts/rest-upload.yaml``.
(c) ``MIGRATION.md`` contains a "Brief 514 — Upload flow" section
    (grep assertion).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


@pytest.mark.asyncio
async def test_legacy_mcp_send_removed(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """MCP tool enumeration confirms ``cassetta_send`` is gone."""
    client, _ = core_app
    api_key = await h.setup_agent(client, "u8", "tool-list")
    sid = await h.mcp_init(client, api_key=api_key)
    headers = dict(MCP_HEADERS)
    headers["Authorization"] = f"Bearer {api_key}"
    headers["mcp-session-id"] = sid
    resp = await client.post(
        "/mcp/",
        json=_jsonrpc("tools/list", {}, req_id=2),
        headers=headers,
    )
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "cassetta_send" not in names
    assert "cassetta_send_init" in names
    assert "cassetta_send_inline" in names


@pytest.mark.asyncio
async def test_legacy_rest_send_returns_410(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """``PUT /inbox/{agent}/{path}`` returns 410 with the structured body."""
    client, _ = core_app
    api_key = await h.setup_agent(client, "u8", "rest-legacy")
    resp = await client.put(
        "/inbox/alice/some-path",
        content=b"legacy body",
        headers=h.auth(api_key),
    )
    assert resp.status_code == 410
    body = resp.json()
    # Match contracts/rest-upload.yaml `legacySendRemoved` exactly.
    assert body == {
        "error": "gone",
        "reason": "replaced_by_514",
        "replacement": "POST /upload/{bundle_path}",
        "migration_guide": "MIGRATION.md#brief-514",
    }


def test_migration_doc_has_brief_514_section() -> None:
    """``MIGRATION.md`` contains the Brief 514 migration heading."""
    # __file__: tests/integration/test_legacy_send_removed.py
    # parents[2] = the repository root
    migration = Path(__file__).resolve().parents[2] / "MIGRATION.md"
    text = migration.read_text(encoding="utf-8")
    assert "Brief 514 — Upload flow" in text, f"Expected 'Brief 514 — Upload flow' heading in {migration}"


# Silence pyright on unused json import when this file is collected alone.
_ = json
