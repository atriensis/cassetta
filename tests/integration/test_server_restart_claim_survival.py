"""The claim sidecar survives a server restart.

Pick a reference-mode bundle → assert sidecar on disk → tear down
and rebuild the app (new FastAPI, SAME ``data/``) → the bundle is
still hidden from the listing; the JWT issued before restart still
works against the download endpoint; successful completion still
deletes the bundle + drops the claim.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from cassetta.app import create_app
from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.mcp_server import configure as configure_mcp

from .conftest import SETUP_TOKEN, CoreHelpers, seed_inbox_bundle


async def _boot_app(storage_dir: str) -> AsyncIterator[httpx.AsyncClient]:
    os.environ["CASSETTA_SETUP_TOKEN"] = SETUP_TOKEN
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ["CASSETTA_MAX_INLINE_SIZE"] = "32"
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    configure_mcp(config, backends)

    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        yield client

    stop.set()
    await task


@pytest.mark.asyncio
async def test_claim_survives_restart() -> None:
    storage_dir = tempfile.mkdtemp()
    h = CoreHelpers()

    # --- First boot: set up, seed, pick, capture state. ---
    async for client in _boot_app(storage_dir):
        sender_key = await h.setup_agent(client, "bob", "main")
        alice_key = await h.create_key(client, sender_key, "alice", "main")

        backend = FilesystemBackend(root_path=storage_dir)
        await seed_inbox_bundle(
            backend,
            "alice:main",
            "survive-me",
            files=[("data.bin", b"S" * 400)],
            sender="bob",
        )

        sid = await h.mcp_init(client, api_key=alice_key)
        pick = await h.mcp_call(
            client,
            "cassetta_pick",
            {"path": "survive-me"},
            sid=sid,
            api_key=alice_key,
        )
        envelope = json.loads(pick["content"][0]["text"])
        assert envelope["mode"] == "reference"

        pre_restart_token = envelope["download_token"]
        dl_path = urllib.parse.urlparse(envelope["files"][0]["url"]).path

    # Sidecar exists on disk after teardown.
    claims_dir = Path(storage_dir) / ".claims"
    sidecars = list(claims_dir.glob("*.json"))
    assert len(sidecars) == 1, f"sidecar should survive app teardown; got {sidecars!r}"

    # --- Second boot: new app, same data/ ---
    async for client in _boot_app(storage_dir):
        # Alice re-authenticates with a fresh session using the same
        # api_key (key store is on disk, survives restart).
        # We need a fresh mcp sid.
        # Listing still hides the claimed bundle.
        resp = await client.get(
            "/inbox/alice:main/",
            headers={"Authorization": f"Bearer {alice_key}"},
        )
        assert resp.status_code == 200
        listing = resp.json()
        paths = {e["path"] for e in listing["files"]}
        assert "survive-me" not in paths, f"bundle should still be hidden after restart; listing: {listing!r}"

        # Pre-restart JWT still works.
        dl = await client.get(
            dl_path,
            headers={
                "Authorization": f"Bearer {pre_restart_token}",
                "X-Sender": "alice:main",
            },
        )
        assert dl.status_code == 200, (dl.status_code, dl.text[:200])
        assert dl.content == b"S" * 400

        # After the successful fetch, the bundle + claim are both gone.
        post_dl_listing = await client.get(
            "/inbox/alice:main/",
            headers={"Authorization": f"Bearer {alice_key}"},
        )
        paths_after = {e["path"] for e in post_dl_listing.json()["files"]}
        assert "survive-me" not in paths_after
        assert list(claims_dir.glob("*.json")) == []
