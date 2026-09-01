"""Brief 541 — serverInfo advertises the cassetta package version.

The MCP ``initialize`` handshake must report ``cassetta.__version__`` in
``serverInfo.version``, not the version of the underlying ``mcp`` SDK.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata

import httpx
import pytest

import cassetta


@pytest.mark.asyncio
async def test_serverinfo_reports_package_version(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    api_key = await h.setup_agent(client, "bob", "version")
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "version-test", "version": "1.0.0"},
        },
    }
    resp = await h.mcp_post(client, body, api_key=api_key)
    assert resp.status_code == 200, resp.text
    server_info = resp.json()["result"]["serverInfo"]
    assert server_info["version"] == cassetta.__version__
    # And explicitly not the mcp SDK version.
    assert server_info["version"] != importlib_metadata.version("mcp")
