"""Pre-bytes rejection for manifest/cap violations.

Each scenario drives `cassetta_send_init` with a manifest that triggers
a specific rejection category and asserts:
(a) the MCP error carries the stable prefix from data-model.md §8,
(b) no bundle directory is created for the would-be path,
(c) validation order: manifest shape first, policy caps second.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest


async def _init_raw(h, client, args: dict, sid: str, api_key: str):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "cassetta_send_init", "arguments": args},
    }
    return await h.mcp_post(client, body, sid=sid, api_key=api_key)


def _error_text(payload: dict) -> str:
    result = payload.get("result") or {}
    if result:
        content = result.get("content") or []
        if content:
            return str(content[0].get("text", ""))
        return json.dumps(result)
    return json.dumps(payload.get("error") or {})


@pytest.mark.asyncio
async def test_traversal_name_rejected_before_policy(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender = await h.setup_agent(client, "bob", "us3-t")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    resp = await _init_raw(
        h,
        client,
        {
            "to": "alice:main",
            "path": "n.md",
            "manifest": {"file_count": 1, "files": [{"name": "../escape", "size": 1}]},
        },
        sid,
        sender,
    )
    text = _error_text(resp.json())
    assert "invalid_manifest" in text
    assert "traversal" in text


@pytest.mark.asyncio
async def test_reserved_name_rejected(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender = await h.setup_agent(client, "bob", "us3-r")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    resp = await _init_raw(
        h,
        client,
        {
            "to": "alice:main",
            "path": "n.md",
            "manifest": {"file_count": 1, "files": [{"name": "meta.json", "size": 1}]},
        },
        sid,
        sender,
    )
    text = _error_text(resp.json())
    assert "invalid_manifest" in text
    assert "reserved_name" in text


@pytest.mark.asyncio
async def test_duplicate_entry_rejected(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender = await h.setup_agent(client, "bob", "us3-d")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    resp = await _init_raw(
        h,
        client,
        {
            "to": "alice:main",
            "path": "n.md",
            "manifest": {
                "file_count": 2,
                "files": [
                    {"name": "foo", "size": 1},
                    {"name": "foo", "size": 1},
                ],
            },
        },
        sid,
        sender,
    )
    text = _error_text(resp.json())
    assert "invalid_manifest" in text
    assert "duplicate" in text


@pytest.mark.asyncio
async def test_prefix_collision_rejected(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender = await h.setup_agent(client, "bob", "us3-p")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    resp = await _init_raw(
        h,
        client,
        {
            "to": "alice:main",
            "path": "n.md",
            "manifest": {
                "file_count": 2,
                "files": [
                    {"name": "src", "size": 1},
                    {"name": "src/main.py", "size": 1},
                ],
            },
        },
        sid,
        sender,
    )
    text = _error_text(resp.json())
    assert "invalid_manifest" in text
    assert "prefix_collision" in text


@pytest.mark.asyncio
async def test_per_file_cap_exceeded(
    core_app: tuple[httpx.AsyncClient, str],
    h,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-file cap rejection raises ``cap_exceeded``."""
    # Shrink cap via env BEFORE fixture boots — but fixture already ran.
    # Instead, patch the policy's config directly.
    client, _ = core_app
    app = client._transport.app  # type: ignore[attr-defined]
    from dataclasses import replace

    app.state.config = replace(
        app.state.config,
        limits=replace(app.state.config.limits, per_file_max=10),
    )
    import cassetta.mcp_server as mcp_module
    from cassetta.defaults.default_limits import CoreLimitsPolicy

    new_policy = CoreLimitsPolicy(app.state.config.limits)
    app.state.backends = replace(app.state.backends, limits_policy=new_policy)
    mcp_module._limits_policy = new_policy

    sender = await h.setup_agent(client, "bob", "us3-cap")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    resp = await _init_raw(
        h,
        client,
        {
            "to": "alice:main",
            "path": "n.md",
            "manifest": {"file_count": 1, "files": [{"name": "big.bin", "size": 100}]},
        },
        sid,
        sender,
    )
    text = _error_text(resp.json())
    assert "cap_exceeded" in text
    assert "per_file_max" in text


@pytest.mark.asyncio
async def test_manifest_invalid_beats_cap_exceeded(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """Scenario 9: when BOTH malformed and over cap, manifest error wins."""
    client, _ = core_app
    app = client._transport.app  # type: ignore[attr-defined]
    from dataclasses import replace

    app.state.config = replace(
        app.state.config,
        limits=replace(app.state.config.limits, per_file_max=10),
    )
    import cassetta.mcp_server as mcp_module
    from cassetta.defaults.default_limits import CoreLimitsPolicy

    new_policy = CoreLimitsPolicy(app.state.config.limits)
    app.state.backends = replace(app.state.backends, limits_policy=new_policy)
    mcp_module._limits_policy = new_policy

    sender = await h.setup_agent(client, "bob", "us3-both")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    resp = await _init_raw(
        h,
        client,
        {
            "to": "alice:main",
            "path": "n.md",
            # Both: duplicate names AND oversize.
            "manifest": {
                "file_count": 2,
                "files": [
                    {"name": "foo", "size": 1000},
                    {"name": "foo", "size": 1000},
                ],
            },
        },
        sid,
        sender,
    )
    text = _error_text(resp.json())
    # Manifest error wins: we see invalid_manifest, not cap_exceeded.
    assert "invalid_manifest" in text
    assert "cap_exceeded" not in text


@pytest.mark.asyncio
async def test_rejected_init_leaves_no_on_disk_residue(
    core_app: tuple[httpx.AsyncClient, str],
    h,
    storage_dir: str,
) -> None:
    """Rejected send_init must not create any bundle directory on disk."""
    # The core_app fixture uses its own tempdir; collect it from config.
    client, _ = core_app
    app = client._transport.app  # type: ignore[attr-defined]
    data_root = os.path.join(app.state.config.storage_path, "data")

    sender = await h.setup_agent(client, "bob", "us3-res")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    await _init_raw(
        h,
        client,
        {
            "to": "alice:main",
            "path": "note.md",
            "manifest": {"file_count": 1, "files": [{"name": "meta.json", "size": 1}]},
        },
        sid,
        sender,
    )
    # Even if data_root exists, the specific bundle dir should not.
    if os.path.isdir(data_root):
        offending = os.path.join(data_root, "inbox", "alice:main", "note.md")
        assert not os.path.exists(offending), f"{offending} must not exist"
