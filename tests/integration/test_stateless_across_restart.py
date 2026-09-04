"""Credentials verify across an app restart that shares the key."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp

SETUP_TOKEN = "integration-test-token"
SHARED_KEY_B64 = "c2hhcmVkLXNoYXJlZC1zaGFyZWQtc2hhcmVkLXNoYXJlZC1zaGFyZWQ="  # 36 raw bytes


async def _make(storage: str) -> AsyncIterator[tuple[httpx.AsyncClient, asyncio.Event, asyncio.Task]]:
    os.environ["CASSETTA_SETUP_TOKEN"] = SETUP_TOKEN
    os.environ["CASSETTA_STORAGE_PATH"] = storage
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_JWT_KEY"] = SHARED_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    app = create_app()
    cfg = app.state.config
    backends = app.state.backends
    configure_mcp(cfg, backends)
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
    ) as c:
        yield c, stop, task


@pytest.mark.asyncio
async def test_credential_verifies_on_sibling_app_with_same_key(
    h,
) -> None:
    """Issue a batch credential on app A; accept it on app B sharing the key + storage."""
    # Shared storage so the bundle path ends up in the same filesystem dir.
    shared_storage = tempfile.mkdtemp()

    # --- App A ----------------------------------------------------------
    async for client_a, stop_a, task_a in _make(shared_storage):
        sender_key = await h.setup_agent(client_a, "bob", "stateless")
        # Force batch.
        os.environ["CASSETTA_MAX_INLINE_SIZE"] = "4"
        try:
            pass
        finally:
            pass
        # Reconfigure policy under the hook.
        from dataclasses import replace

        client_a._transport.app.state.config = replace(  # type: ignore[attr-defined]
            client_a._transport.app.state.config,  # type: ignore[attr-defined]
            limits=replace(client_a._transport.app.state.config.limits, max_inline_size=4),  # type: ignore[attr-defined]
        )
        import cassetta.mcp_server as mcp_module
        from cassetta.defaults.default_limits import CoreLimitsPolicy

        policy = CoreLimitsPolicy(client_a._transport.app.state.config.limits)  # type: ignore[attr-defined]
        client_a._transport.app.state.backends = replace(  # type: ignore[attr-defined]
            client_a._transport.app.state.backends,  # type: ignore[attr-defined]
            limits_policy=policy,
        )
        mcp_module._limits_policy = policy

        await h.create_key(client_a, sender_key, "alice", "main")
        sid_a = await h.mcp_init(client_a, api_key=sender_key)
        init = await h.mcp_call(
            client_a,
            "cassetta_send_init",
            {
                "to": "alice:main",
                "path": "cross.bin",
                "manifest": {
                    "file_count": 1,
                    "files": [{"name": "payload.bin", "size": 50}],
                },
            },
            sid=sid_a,
            api_key=sender_key,
        )
        body = json.loads(init["content"][0]["text"])
        batch_token = body["batch_token"]
        bundle_id = body["bundle_id"]
        stop_a.set()
        await task_a
        break

    # --- App B (fresh process, same key + same storage) ----------------
    os.environ.pop("CASSETTA_MAX_INLINE_SIZE", None)
    async for client_b, stop_b, task_b in _make(shared_storage):
        import gzip
        import io
        import tarfile
        import urllib.parse

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("payload.bin")
            info.size = 50
            tf.addfile(info, io.BytesIO(b"y" * 50))
        tar_raw = buf.getvalue()
        tar_gz = gzip.compress(tar_raw)

        # Reconstruct the upload path from the bundle_path in the JWT
        # (unchanged across restart).
        bundle_path = "inbox/alice:main/cross.bin"
        url_path = "/upload/" + urllib.parse.quote(bundle_path, safe="")
        resp = await client_b.post(
            url_path,
            content=tar_gz,
            headers={
                "Authorization": f"Bearer {batch_token}",
                "Content-Type": "application/x-tar",
                "Content-Encoding": "gzip",
            },
        )
        assert resp.status_code == 201, (resp.status_code, resp.text)
        assert resp.json()["bundle_id"] == bundle_id
        stop_b.set()
        await task_b
        break
