import os
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.app import create_app
from cassetta.backends.filesystem.storage import FilesystemBackend


@pytest.fixture
async def ttl_client(
    storage_dir: str,
) -> AsyncIterator[httpx.AsyncClient]:
    """Client with a short TTL (1 second)."""
    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "1"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"

    app = create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


class TestTTLExpiry:
    async def test_file_accessible_within_ttl(
        self, ttl_client: httpx.AsyncClient
    ) -> None:
        await ttl_client.put("/files/fresh.txt", content=b"fresh")
        response = await ttl_client.get("/files/fresh.txt")
        assert response.status_code == 200
        envelope = response.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "fresh"

    async def test_expired_file_returns_404(
        self, ttl_client: httpx.AsyncClient
    ) -> None:
        await ttl_client.put("/files/expiring.txt", content=b"bye")
        # Wait for TTL to expire (1 second TTL)
        time.sleep(1.5)
        response = await ttl_client.get("/files/expiring.txt")
        assert response.status_code == 404

    async def test_expired_files_excluded_from_list(
        self, ttl_client: httpx.AsyncClient
    ) -> None:
        await ttl_client.put("/files/old.txt", content=b"old")
        time.sleep(1.5)
        # Upload a fresh file after the old one expired
        await ttl_client.put("/files/new.txt", content=b"new")
        response = await ttl_client.get("/files/")
        files = response.json()["files"]
        paths = [f["path"] for f in files]
        assert "new.txt" in paths
        assert "old.txt" not in paths

    async def test_no_expiry_when_ttl_zero(
        self, client: httpx.AsyncClient
    ) -> None:
        # client fixture has TTL=0
        await client.put("/files/forever.txt", content=b"permanent")
        time.sleep(0.1)
        response = await client.get("/files/forever.txt")
        assert response.status_code == 200


class TestTTLCleanup:
    async def test_cleanup_removes_expired_bundles(
        self, storage_dir: str
    ) -> None:
        """One cleanup sweep removes expired bundles in the store namespace."""
        import io
        import uuid
        from datetime import UTC, datetime

        from cassetta.config import AppConfig

        backend = FilesystemBackend(root_path=storage_dir)
        # Seed a committed bundle
        writer = await backend.open_bundle_write("store/to-expire.txt")
        await writer.write_file("to-expire.txt", io.BytesIO(b"data"))
        await writer.commit({
            "schema_version": 1,
            "bundle_id": uuid.uuid4().hex,
            "sender": None,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": 1,
            "files": [{
                "name": "to-expire.txt", "size": 4, "mime": "text/plain",
            }],
        })
        time.sleep(1.5)

        config = AppConfig(
            setup_token="",
            dev_mode=True,
            storage_path=storage_dir,
            keys_file=os.path.join(storage_dir, ".cassetta-keys.json"),
            default_ttl=1,
            allowed_path_chars=r"a-zA-Z0-9\-_./",
            mcp_allowed_hosts=(),
        )

        now = time.time()
        for ref in backend.list_bundles("store/"):
            meta = await backend.read_bundle_meta(ref.path)
            created = datetime.fromisoformat(str(meta["created_at"]))
            if now - created.timestamp() > config.default_ttl:
                await backend.delete_bundle(ref.path)

        with pytest.raises(FileNotFoundError):
            await backend.read_bundle_meta("store/to-expire.txt")
