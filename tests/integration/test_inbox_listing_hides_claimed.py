"""Claimed bundles are excluded from inbox listings.

After a reference-mode pick writes a claim sidecar, both the MCP
``cassetta_inbox`` tool and the REST ``GET /inbox/{agent}/`` listing
MUST NOT contain the bundle. Manually deleting the sidecar restores
visibility in the same listing call (proving the filter reads the
sidecar on every listing call, no caching).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend

from .conftest import seed_inbox_bundle


def _unwrap(result: dict) -> dict | list:
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_listing_excludes_claimed_bundle(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app_small_inline
    sender = await h.setup_agent(client, "bob", "main")
    alice_key = await h.create_key(client, sender, "alice", "main")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    # Seed two bundles: the one we'll claim, and a control that stays
    # visible throughout.
    await seed_inbox_bundle(
        backend,
        "alice:main",
        "claimed-bundle",
        files=[("a.bin", b"A" * 200)],
        sender="bob",
    )
    await seed_inbox_bundle(
        backend,
        "alice:main",
        "control-bundle",
        files=[("c.bin", b"C" * 200)],
        sender="bob",
    )

    sid = await h.mcp_init(client, api_key=alice_key)
    # Baseline: both visible.
    listing = _unwrap(
        await h.mcp_call(
            client,
            "cassetta_inbox",
            {"agent": "alice:main"},
            sid=sid,
            api_key=alice_key,
        )
    )
    assert isinstance(listing, list)
    paths_before = {e["path"] for e in listing}
    assert "claimed-bundle" in paths_before
    assert "control-bundle" in paths_before

    # Pick (reference mode) → claim sidecar written.
    pick = await h.mcp_call(
        client,
        "cassetta_pick",
        {"path": "claimed-bundle"},
        sid=sid,
        api_key=alice_key,
    )
    envelope = json.loads(pick["content"][0]["text"])
    assert envelope["mode"] == "reference"

    # MCP listing now excludes the claimed bundle.
    listing_after = _unwrap(
        await h.mcp_call(
            client,
            "cassetta_inbox",
            {"agent": "alice:main"},
            sid=sid,
            api_key=alice_key,
        )
    )
    assert isinstance(listing_after, list)
    paths_after = {e["path"] for e in listing_after}
    assert "claimed-bundle" not in paths_after
    assert "control-bundle" in paths_after

    # REST listing also excludes it.
    rest = await client.get(
        "/inbox/alice:main/",
        headers={"Authorization": f"Bearer {alice_key}"},
    )
    assert rest.status_code == 200
    rest_paths = {e["path"] for e in rest.json()["files"]}
    assert "claimed-bundle" not in rest_paths
    assert "control-bundle" in rest_paths

    # Manually drop the sidecar → the bundle reappears in the listing.
    claims_dir = storage_root / ".claims"
    sidecars = list(claims_dir.glob("*.json"))
    assert len(sidecars) == 1
    sidecars[0].unlink()

    listing_after_drop = _unwrap(
        await h.mcp_call(
            client,
            "cassetta_inbox",
            {"agent": "alice:main"},
            sid=sid,
            api_key=alice_key,
        )
    )
    assert isinstance(listing_after_drop, list)
    paths_restored = {e["path"] for e in listing_after_drop}
    assert "claimed-bundle" in paths_restored
