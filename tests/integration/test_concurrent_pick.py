"""Concurrent picks serialize through the claim sidecar.

Two async picks on the same unclaimed bundle fire via ``asyncio.gather``.
Exactly one MUST win (reference envelope + sidecar on
disk); the other MUST see the same "bundle not found" surface a
late-comer would see. The loser's in-memory JWT MUST NOT leak to the
caller.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend

from .conftest import seed_inbox_bundle


@pytest.mark.asyncio
async def test_concurrent_pick_serializes(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app_small_inline
    sender = await h.setup_agent(client, "bob", "main")
    alice_key = await h.create_key(client, sender, "alice", "main")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    await seed_inbox_bundle(
        backend,
        "alice:main",
        "race-bundle",
        files=[("data.bin", b"D" * 400)],
        sender="bob",
    )

    sid = await h.mcp_init(client, api_key=alice_key)

    async def do_pick() -> dict:
        return await h.mcp_call(
            client,
            "cassetta_pick",
            {"path": "race-bundle"},
            sid=sid,
            api_key=alice_key,
        )

    # Both picks race concurrently.
    r1, r2 = await asyncio.gather(do_pick(), do_pick())

    outcomes = [r1, r2]
    winners = []
    losers = []
    for r in outcomes:
        if r.get("isError"):
            losers.append(r)
            continue
        text = r["content"][0]["text"]
        try:
            env = json.loads(text)
        except json.JSONDecodeError:
            losers.append(r)
            continue
        if isinstance(env, dict) and env.get("mode") == "reference":
            winners.append(env)
        else:
            # Inline result — test misconfigured (bundle not large enough).
            losers.append(r)

    assert len(winners) == 1, f"expected exactly one winner, got {winners!r}"
    assert len(losers) == 1, f"expected exactly one loser, got {losers!r}"

    # Winner's sidecar is on disk.
    claims_dir = storage_root / ".claims"
    sidecars = list(claims_dir.glob("*.json"))
    assert len(sidecars) == 1, f"expected exactly one sidecar, got {sidecars!r}"

    # The loser's response MUST NOT leak the file bytes or the
    # would-be JWT — the error surface is a plain "File not found"
    # ValueError (propagated through MCP as an isError content item).
    loser_text = losers[0]["content"][0]["text"]
    assert "File not found" in loser_text or "not_found" in loser_text
    assert "eyJ" not in loser_text  # No JWT header prefix leaked.
