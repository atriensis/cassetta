"""Brief 541 — guard: public MCP tool descriptions must not leak internals.

Sibling lock to the §VII layer-separation AST test. FastMCP surfaces each
tool's docstring as the public ``description`` in ``tools/list``; this test
keeps internal change-ticket numbers, requirement IDs, and internal
class/policy names out of the send-flow + capabilities descriptions.
"""

from __future__ import annotations

import re

import httpx
import pytest

_FORBIDDEN_SUBSTRINGS = [
    "Brief ",
    "LimitsPolicy",
    "evaluate_upload",
    "check_manifest_against_limits",
    "_cassetta_",
    "_decode_inline_content",
]
_FR_RE = re.compile(r"\bFR-\d")
_SEND_TOOLS = ("cassetta_send_init", "cassetta_send_inline", "cassetta_capabilities")


async def _list_tools(h, client: httpx.AsyncClient, api_key: str) -> dict:
    sid = await h.mcp_init(client, api_key=api_key)
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    resp = await h.mcp_post(client, body, sid=sid, api_key=api_key)
    assert resp.status_code == 200, resp.text
    return {t["name"]: t for t in resp.json()["result"]["tools"]}


@pytest.mark.asyncio
async def test_send_descriptions_have_no_internal_leakage(
    core_app: tuple[httpx.AsyncClient, str], h,
) -> None:
    client, _ = core_app
    key = await h.setup_agent(client, "bob", "hygiene")
    tools = await _list_tools(h, client, key)
    for name in _SEND_TOOLS:
        desc = tools[name].get("description") or ""
        for bad in _FORBIDDEN_SUBSTRINGS:
            assert bad not in desc, f"{name} description leaks {bad!r}: {desc!r}"
        assert not _FR_RE.search(desc), f"{name} description leaks an FR-id: {desc!r}"


@pytest.mark.asyncio
async def test_send_descriptions_state_caller_usage(
    core_app: tuple[httpx.AsyncClient, str], h,
) -> None:
    """Descriptions must read as caller guidance (inputs + the encoding constraint)."""
    client, _ = core_app
    key = await h.setup_agent(client, "bob", "hygiene2")
    tools = await _list_tools(h, client, key)
    init_desc = (tools["cassetta_send_init"].get("description") or "").lower()
    inline_desc = (tools["cassetta_send_inline"].get("description") or "").lower()
    assert "manifest" in init_desc
    assert "encoding" in inline_desc
