"""Tests for REST inbox endpoints."""

import asyncio
import os
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.app import create_app
from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.mcp_server import configure as configure_mcp

from .conftest import seed_inbox_bundle


@pytest.fixture
async def inbox_client(
    storage_dir: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, FilesystemBackend, str, str]]:
    """Client with auth enabled + two API keys (alice, bob).

    Returns (client, backend, alice_key, bob_key).
    """
    setup_token = "test-inbox-setup"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_JWT_KEY"] = (
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    )
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    backend = backends.backend
    key_store = backends.key_store
    configure_mcp(config, backends)

    # Create keys for alice and bob
    alice_key, _ = await key_store.setup("test:alice")
    bob_key, _ = await key_store.create_key("test:bob")

    # Start MCP session manager
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
        yield c, backend, alice_key, bob_key

    stop.set()
    await task


@pytest.fixture
async def inbox_ttl_client(
    storage_dir: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, FilesystemBackend, str, str]]:
    """Client with TTL=1 for inbox expiry tests."""
    setup_token = "test-inbox-ttl"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "1"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_JWT_KEY"] = (
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    )
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    backend = backends.backend
    key_store = backends.key_store
    configure_mcp(config, backends)

    alice_key, _ = await key_store.setup("test:alice")
    bob_key, _ = await key_store.create_key("test:bob")

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
        yield c, backend, alice_key, bob_key

    stop.set()
    await task


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ============================================================
# US2: List Inbox
# ============================================================


class TestInboxList:
    async def test_list_returns_files_newest_first(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, _alice_key, bob_key = inbox_client
        await seed_inbox_bundle(
            backend, "test:bob", "first.txt", content=b"1", sender="test:alice",
        )
        time.sleep(0.05)
        await seed_inbox_bundle(
            backend, "test:bob", "second.txt", content=b"2", sender="test:alice",
        )
        time.sleep(0.05)
        await seed_inbox_bundle(
            backend, "test:bob", "third.txt", content=b"3", sender="test:alice",
        )

        response = await client.get("/inbox/test:bob/", headers=_auth(bob_key))
        assert response.status_code == 200
        data = response.json()
        assert data["agent"] == "test:bob"
        files = data["files"]
        assert len(files) == 3
        # Newest first
        assert files[0]["path"] == "third.txt"
        assert files[1]["path"] == "second.txt"
        assert files[2]["path"] == "first.txt"
        # Each has sender
        for f in files:
            assert f["sender"] == "test:alice"
            assert "created_at" in f
            assert "remaining_ttl" in f

    async def test_list_empty_inbox(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, _backend, _alice_key, bob_key = inbox_client
        response = await client.get("/inbox/test:bob/", headers=_auth(bob_key))
        assert response.status_code == 200
        assert response.json()["files"] == []

    async def test_list_other_agents_inbox(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, alice_key, _bob_key = inbox_client
        # Alice sends to bob
        await seed_inbox_bundle(
            backend, "test:bob", "secret.txt", content=b"data", sender="test:alice",
        )
        # Alice can list bob's inbox (core: no access control)
        response = await client.get("/inbox/test:bob/", headers=_auth(alice_key))
        assert response.status_code == 200
        assert len(response.json()["files"]) == 1

    async def test_list_with_remaining_ttl(
        self, inbox_ttl_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, _alice_key, bob_key = inbox_ttl_client
        await seed_inbox_bundle(
            backend, "test:bob", "msg.txt", content=b"data", sender="test:alice",
        )
        response = await client.get("/inbox/test:bob/", headers=_auth(bob_key))
        files = response.json()["files"]
        assert len(files) == 1
        assert files[0]["remaining_ttl"] is not None
        assert files[0]["remaining_ttl"] >= 0


# ============================================================
# US3: Pick / Get / Delete
# ============================================================


class TestInboxPick:
    async def test_pick_returns_content_and_deletes(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, _alice_key, bob_key = inbox_client
        await seed_inbox_bundle(
            backend, "test:bob", "task.json",
            content=b'{"do": "this"}', sender="test:alice",
        )
        response = await client.post(
            "/inbox/test:bob/task.json/pick", headers=_auth(bob_key),
        )
        assert response.status_code == 200
        envelope = response.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == '{"do": "this"}'
        assert response.headers.get("x-cassetta-sender") == "test:alice"
        assert response.headers.get("x-cassetta-path") == "task.json"

        # Verify bundle directory removed
        with pytest.raises(FileNotFoundError):
            await backend.read_bundle_meta("inbox/test:bob/task.json")

    async def test_pick_nonexistent_returns_404(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, _backend, _alice_key, bob_key = inbox_client
        response = await client.post(
            "/inbox/test:bob/missing.txt/pick", headers=_auth(bob_key),
        )
        assert response.status_code == 404

    async def test_picked_file_not_in_list(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, _alice_key, bob_key = inbox_client
        await seed_inbox_bundle(
            backend, "test:bob", "msg.txt", content=b"hello", sender="test:alice",
        )
        await client.post("/inbox/test:bob/msg.txt/pick", headers=_auth(bob_key))
        response = await client.get("/inbox/test:bob/", headers=_auth(bob_key))
        assert response.json()["files"] == []


class TestInboxGet:
    async def test_get_returns_content_without_delete(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, _alice_key, bob_key = inbox_client
        await seed_inbox_bundle(
            backend, "test:bob", "doc.txt", content=b"read me", sender="test:alice",
        )
        response = await client.get(
            "/inbox/test:bob/doc.txt", headers=_auth(bob_key),
        )
        assert response.status_code == 200
        envelope = response.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "read me"
        assert response.headers.get("x-cassetta-sender") == "test:alice"

        # Bundle still exists after non-destructive read
        meta = await backend.read_bundle_meta("inbox/test:bob/doc.txt")
        assert meta["sender"] == "test:alice"

    async def test_get_missing_returns_404(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, _backend, _alice_key, bob_key = inbox_client
        response = await client.get(
            "/inbox/test:bob/nope.txt", headers=_auth(bob_key),
        )
        assert response.status_code == 404


class TestInboxDelete:
    async def test_delete_returns_204(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, _alice_key, bob_key = inbox_client
        await seed_inbox_bundle(
            backend, "test:bob", "temp.txt", content=b"temp", sender="test:alice",
        )
        response = await client.delete(
            "/inbox/test:bob/temp.txt", headers=_auth(bob_key),
        )
        assert response.status_code == 204
        with pytest.raises(FileNotFoundError):
            await backend.read_bundle_meta("inbox/test:bob/temp.txt")

    async def test_delete_missing_returns_404(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, _backend, _alice_key, bob_key = inbox_client
        response = await client.delete(
            "/inbox/test:bob/nope.txt", headers=_auth(bob_key),
        )
        assert response.status_code == 404


# ============================================================
# US6: Latest Alias
# ============================================================


class TestLatestAlias:
    async def test_pick_latest_returns_newest(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, _alice_key, bob_key = inbox_client
        await seed_inbox_bundle(
            backend, "test:bob", "old.txt", content=b"old", sender="test:alice",
        )
        time.sleep(0.05)
        await seed_inbox_bundle(
            backend, "test:bob", "new.txt", content=b"newest", sender="test:alice",
        )

        response = await client.post(
            "/inbox/test:bob/latest/pick", headers=_auth(bob_key),
        )
        assert response.status_code == 200
        envelope = response.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "newest"
        assert response.headers.get("x-cassetta-path") == "new.txt"

    async def test_pick_latest_empty_inbox_returns_404(
        self, inbox_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, _backend, _alice_key, bob_key = inbox_client
        response = await client.post(
            "/inbox/test:bob/latest/pick", headers=_auth(bob_key),
        )
        assert response.status_code == 404


# ============================================================
# US7: TTL for Inbox
# ============================================================


class TestInboxTTL:
    async def test_expired_inbox_file_excluded_from_list(
        self, inbox_ttl_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, _alice_key, bob_key = inbox_ttl_client
        await seed_inbox_bundle(
            backend, "test:bob", "expiring.txt", content=b"bye", sender="test:alice",
        )
        time.sleep(1.1)
        response = await client.get("/inbox/test:bob/", headers=_auth(bob_key))
        assert response.json()["files"] == []

    async def test_pick_expired_inbox_file_returns_404(
        self, inbox_ttl_client: tuple[httpx.AsyncClient, FilesystemBackend, str, str]
    ) -> None:
        client, backend, _alice_key, bob_key = inbox_ttl_client
        await seed_inbox_bundle(
            backend, "test:bob", "temp.txt", content=b"temp", sender="test:alice",
        )
        time.sleep(1.1)
        response = await client.post(
            "/inbox/test:bob/temp.txt/pick", headers=_auth(bob_key),
        )
        assert response.status_code == 404
