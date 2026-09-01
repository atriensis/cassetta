"""Integration tests for GET /inbox/.../peek + GET /files/.../peek (brief 513 US1)."""

from __future__ import annotations

import pytest

from cassetta.protocols.identity import Identity


async def _seed_inbox(
    backend, agent: str, path: str, data: bytes, sender: str | None = None,
) -> None:
    """Write a bundle directly via the backend (bypasses alias resolver)."""
    import io
    from datetime import UTC, datetime

    writer = await backend.open_bundle_write(f"inbox/{agent}/{path}")
    try:
        await writer.write_file(path, io.BytesIO(data))
        meta = {
            "schema_version": 1,
            "bundle_id": "seed",
            "sender": sender,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": 1,
            "files": [{"name": path, "size": len(data), "mime": "text/plain"}],
        }
        await writer.commit(meta)
    except Exception:
        await writer.abort()
        raise


@pytest.mark.asyncio
async def test_peek_inbox_round_trip(client) -> None:  # noqa: ANN001
    backend = client._transport.app.state.backends.backend  # type: ignore[attr-defined]
    await _seed_inbox(backend, "alice", "notes.md", b"hello world", sender="bob")

    resp = await client.get("/inbox/alice/notes.md/peek")
    assert resp.status_code == 200
    payload = resp.json()
    assert "bundle" in payload
    assert payload["bundle"]["file_count"] == 1
    assert payload["bundle"]["files"][0]["name"] == "notes.md"
    assert payload["bundle"]["files"][0]["size"] == len(b"hello world")
    assert payload["bundle"]["sender"] == "bob"
    assert resp.headers.get("X-Cassetta-File-Count") == "1"
    assert resp.headers.get("X-Cassetta-Sender") == "bob"

    # Subsequent list still shows the bundle
    listing = await client.get("/inbox/alice/")
    assert listing.status_code == 200
    assert any(
        f["path"] == "notes.md" for f in listing.json().get("files", [])
    )

    # Subsequent pick returns the original bytes (unified inline envelope)
    pick = await client.post("/inbox/alice/notes.md/pick")
    assert pick.status_code == 200
    pick_envelope = pick.json()
    assert pick_envelope["mode"] == "inline"
    assert pick_envelope["files"][0]["content"] == "hello world"


@pytest.mark.asyncio
async def test_peek_store_round_trip(client) -> None:  # noqa: ANN001
    await client.put("/files/proj/readme.md", content=b"project content")

    resp = await client.get("/files/proj/readme.md/peek")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["bundle"]["file_count"] == 1
    assert payload["bundle"]["files"][0]["name"] == "readme.md"
    assert resp.headers.get("X-Cassetta-File-Count") == "1"

    listing = await client.get("/files/")
    assert listing.status_code == 200
    paths = [f["path"] for f in listing.json()["files"]]
    assert "proj/readme.md" in paths

    dl = await client.get("/files/proj/readme.md")
    assert dl.status_code == 200
    dl_envelope = dl.json()
    assert dl_envelope["mode"] == "inline"
    assert dl_envelope["files"][0]["content"] == "project content"


@pytest.mark.asyncio
async def test_peek_inbox_unknown_path_404(client) -> None:  # noqa: ANN001
    resp = await client.get("/inbox/alice/ghost.md/peek")
    assert resp.status_code == 404
    assert "File not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_peek_store_unknown_path_404(client) -> None:  # noqa: ANN001
    resp = await client.get("/files/ghost/readme.md/peek")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_peek_inbox_orphan_404(client, storage_dir: str) -> None:  # noqa: ANN001
    import os

    orphan_dir = os.path.join(storage_dir, "data", "inbox", "alice", "orphan.md")
    os.makedirs(orphan_dir, exist_ok=True)
    with open(os.path.join(orphan_dir, "inner.txt"), "wb") as f:
        f.write(b"x")

    resp = await client.get("/inbox/alice/orphan.md/peek")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_peek_respects_access_policy(storage_dir: str) -> None:
    import asyncio
    import os

    import httpx

    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1"
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    from dataclasses import replace
    from typing import ClassVar

    from cassetta.app import create_app
    from cassetta.mcp_server import configure as configure_mcp

    class _DenyPeek:
        kind: ClassVar[str] = "deny-peek"

        async def check(
            self, identity: Identity, resource: str, action: str,
        ) -> bool:
            return action != "peek"

    app = create_app()
    config = app.state.config
    app.state.backends = replace(app.state.backends, access_policy=_DenyPeek())
    backends = app.state.backends
    backend = backends.backend
    configure_mcp(config, backends)

    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()

    try:
        await _seed_inbox(backend, "alice", "private.md", b"secret")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:16001",
        ) as c:
            resp = await c.get("/inbox/alice/private.md/peek")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "Forbidden"
    finally:
        stop.set()
        await task
