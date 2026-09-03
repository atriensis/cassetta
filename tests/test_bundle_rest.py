"""Tests for REST endpoint multi-file bundle operations."""

import tempfile
from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp

from .conftest import seed_inbox_bundle


@pytest.fixture
def rest_storage_dir() -> str:
    return tempfile.mkdtemp()


@pytest.fixture
def rest_env(rest_storage_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # monkeypatch auto-restores; cap vars are cleared so a value
    # leaked by another test can't alter this test's bundle-size behavior
    # (Principle IX).
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", rest_storage_dir)
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.setenv(
        "CASSETTA_MCP_ALLOWED_HOSTS",
        "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001",
    )
    monkeypatch.setenv(
        "CASSETTA_JWT_KEY",
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0",
    )
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    for var in (
        "CASSETTA_KEYS_FILE",
        "CASSETTA_MAX_FILE_SIZE",
        "CASSETTA_PER_FILE_MAX",
        "CASSETTA_PER_BUNDLE_TOTAL_MAX",
        "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
        "CASSETTA_MAX_INLINE_SIZE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
async def rest_app(rest_env: None, rest_storage_dir: str) -> AsyncIterator[tuple]:
    """Return (app, httpx client) for REST bundle testing."""
    app = create_app()
    config = app.state.config
    backends = app.state.backends
    configure_mcp(config, backends)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as c:
        yield app, c


@pytest.fixture
async def rest_client(rest_app: tuple) -> AsyncIterator[httpx.AsyncClient]:
    """Convenience fixture: just the client (for tests that don't need the app)."""
    _app, client = rest_app
    # Register a dev:agent key for alias resolution
    await _app.state.backends.key_store.create_key("dev:agent")
    yield client


# ============================================================
# REST pick bundle (seeded directly — the PUT send endpoint is gone)
# ============================================================


class TestRestPickBundle:
    @pytest.mark.asyncio
    async def test_pick_bundle_returns_json(self, rest_app: tuple) -> None:
        """POST .../pick on multi-file bundle returns JSON."""
        app, client = rest_app
        await app.state.backends.key_store.create_key("dev:agent")
        await seed_inbox_bundle(
            app.state.backends.backend,
            "dev:agent",
            "my-bundle",
            files=[("plan.md", b"# Plan"), ("config.yaml", b"key: val")],
        )

        resp = await client.post("/inbox/dev:agent/my-bundle/pick")
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        data = resp.json()
        assert isinstance(data["bundle"], dict)
        assert data["bundle"]["schema_version"] == 1
        assert data["bundle"]["file_count"] == 2
        assert len(data["files"]) == 2
        assert data["files"][0]["name"] == "plan.md"

    @pytest.mark.asyncio
    async def test_pick_single_file_unified_envelope(self, rest_app: tuple) -> None:
        """Pick single-file returns unified inline envelope."""
        app, client = rest_app
        await app.state.backends.key_store.create_key("dev:agent")
        await seed_inbox_bundle(
            app.state.backends.backend,
            "dev:agent",
            "note.txt",
            content=b"hello",
        )

        resp = await client.post("/inbox/dev:agent/note.txt/pick")
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        envelope = resp.json()
        assert envelope["mode"] == "inline"
        assert isinstance(envelope["bundle"], dict)
        assert envelope["bundle"]["file_count"] == 1
        assert len(envelope["files"]) == 1
        assert envelope["files"][0]["name"] == "note.txt"
        assert envelope["files"][0]["content"] == "hello"
        assert envelope["files"][0]["encoding"] == "utf8"


# ============================================================
# T026: REST inbox listing with file_count (US3)
# ============================================================


class TestRestInboxListing:
    @pytest.mark.asyncio
    async def test_inbox_listing_includes_file_count(self, rest_app: tuple) -> None:
        """GET /inbox/{agent}/ includes file_count in items."""
        app, client = rest_app
        await app.state.backends.key_store.create_key("dev:agent")
        await seed_inbox_bundle(
            app.state.backends.backend,
            "dev:agent",
            "note.txt",
            content=b"hello",
        )
        await seed_inbox_bundle(
            app.state.backends.backend,
            "dev:agent",
            "my-bundle",
            files=[("a.txt", b"aaa"), ("b.txt", b"bbb")],
        )

        resp = await client.get("/inbox/dev:agent/")
        assert resp.status_code == 200
        data = resp.json()
        by_path = {f["path"]: f for f in data["files"]}
        assert by_path["note.txt"]["file_count"] == 1
        assert by_path["my-bundle"]["file_count"] == 2


# ============================================================
# T032-T034: REST files namespace bundles (US4)
# ============================================================


class TestRestFilesBundles:
    @pytest.mark.asyncio
    async def test_put_bundle_multipart(self, rest_client: httpx.AsyncClient) -> None:
        """T032: PUT /files/{path} with multipart creates bundle."""
        files = [
            ("files", ("src/main.py", b"print('hi')", "application/octet-stream")),
            ("files", ("README.md", b"# Readme", "application/octet-stream")),
        ]
        resp = await rest_client.put("/files/my-package", files=files)
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_get_bundle_returns_json(self, rest_client: httpx.AsyncClient) -> None:
        """T033: GET /files/{path} for bundle returns JSON."""
        files = [
            ("files", ("plan.md", b"# Plan", "application/octet-stream")),
            ("files", ("code.py", b"x = 1", "application/octet-stream")),
        ]
        await rest_client.put("/files/test-bundle", files=files)

        resp = await rest_client.get("/files/test-bundle")
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        data = resp.json()
        assert isinstance(data["bundle"], dict)
        assert data["bundle"]["schema_version"] == 1
        assert data["bundle"]["file_count"] == 2
        assert len(data["files"]) == 2

    @pytest.mark.asyncio
    async def test_list_files_includes_file_count(self, rest_client: httpx.AsyncClient) -> None:
        """T034: GET /files/ includes file_count."""
        # Single file
        await rest_client.put("/files/single.txt", content=b"solo")

        # Bundle
        files = [
            ("files", ("a.txt", b"aaa", "application/octet-stream")),
            ("files", ("b.txt", b"bbb", "application/octet-stream")),
        ]
        await rest_client.put("/files/multi-bundle", files=files)

        resp = await rest_client.get("/files/")
        assert resp.status_code == 200
        data = resp.json()
        by_path = {f["path"]: f for f in data["files"]}
        assert by_path["single.txt"]["file_count"] == 1
        assert by_path["multi-bundle"]["file_count"] == 2


# ============================================================
# T042: REST broadcast bundle (US5)
# ============================================================


class TestRestBroadcastBundle:
    @pytest.mark.asyncio
    async def test_broadcast_bundle_multipart(self, rest_client: httpx.AsyncClient) -> None:
        """T042: POST /broadcast with multipart sends bundle to all agents."""
        # Register agents
        await rest_client.post("/keys", json={"host": "alice", "project": "proj"})
        await rest_client.post("/keys", json={"host": "bob", "project": "proj"})

        files = [
            ("files", ("update.md", b"# Update", "application/octet-stream")),
            ("files", ("data.csv", b"a,b,c", "application/octet-stream")),
        ]
        resp = await rest_client.post("/broadcast/team-update", files=files)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_delivered"] >= 2
