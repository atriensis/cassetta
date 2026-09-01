import os

import httpx
import pytest

from cassetta.app import create_app
from cassetta.auth import FileKeyStore


@pytest.fixture
def key_store(storage_dir: str) -> FileKeyStore:
    keys_file = os.path.join(storage_dir, ".cassetta-keys.json")
    return FileKeyStore(keys_file=keys_file)


class TestKeyStore:
    async def test_create_key(self, key_store: FileKeyStore) -> None:
        raw_key, info = await key_store.create_key("test-agent")
        assert raw_key.startswith("cst_")
        assert info.label == "test-agent"
        assert info.is_active is True

    async def test_validate_valid_key(self, key_store: FileKeyStore) -> None:
        raw_key, _ = await key_store.create_key("agent")
        info = await key_store.validate(raw_key)
        assert info is not None
        assert info.label == "agent"

    async def test_validate_invalid_key(self, key_store: FileKeyStore) -> None:
        info = await key_store.validate("cst_invalid_key_value")
        assert info is None

    async def test_revoke_key(self, key_store: FileKeyStore) -> None:
        raw_key, _ = await key_store.create_key("to-revoke")
        await key_store.revoke_key("to-revoke")
        info = await key_store.validate(raw_key)
        assert info is None

    async def test_rotate_key(self, key_store: FileKeyStore) -> None:
        old_key, _ = await key_store.create_key("rotate-me")
        new_key, info = await key_store.rotate_key("rotate-me")
        assert new_key != old_key
        assert info.label == "rotate-me"
        assert await key_store.validate(old_key) is None
        assert await key_store.validate(new_key) is not None

    async def test_list_keys(self, key_store: FileKeyStore) -> None:
        await key_store.create_key("a")
        await key_store.create_key("b")
        keys = await key_store.list_keys()
        labels = [k.label for k in keys]
        assert "a" in labels
        assert "b" in labels

    async def test_duplicate_label_raises(self, key_store: FileKeyStore) -> None:
        await key_store.create_key("dup")
        with pytest.raises(ValueError, match="already exists"):
            await key_store.create_key("dup")

    async def test_revoke_missing_raises(self, key_store: FileKeyStore) -> None:
        with pytest.raises(KeyError):
            await key_store.revoke_key("nonexistent")

    async def test_rotate_missing_raises(self, key_store: FileKeyStore) -> None:
        with pytest.raises(KeyError):
            await key_store.rotate_key("nonexistent")


class TestAuthEndpoint:
    async def test_dev_mode_skips_auth(self) -> None:
        os.environ["CASSETTA_SETUP_TOKEN"] = ""
        os.environ["CASSETTA_STORAGE_PATH"] = "/tmp/test-auth"
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/health")
            assert response.status_code == 200

    async def test_missing_auth_header_returns_401(
        self, auth_client: tuple[httpx.AsyncClient, str]
    ) -> None:
        client, setup_token = auth_client
        # First create a key via setup
        response = await client.post(
            "/setup",
            json={"host": "test", "project": "test"},
            headers={"X-Setup-Token": setup_token},
        )
        assert response.status_code == 201
        # Now try file operation without auth
        response = await client.put(
            "/files/test.txt",
            content=b"data",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code in (401, 403)

    async def test_invalid_key_returns_401(
        self, auth_client: tuple[httpx.AsyncClient, str]
    ) -> None:
        client, setup_token = auth_client
        # Setup first
        await client.post(
            "/setup",
            json={"host": "test", "project": "test"},
            headers={"X-Setup-Token": setup_token},
        )
        response = await client.put(
            "/files/test.txt",
            content=b"data",
            headers={
                "Authorization": "Bearer cst_invalid_key",
                "Content-Type": "application/octet-stream",
            },
        )
        assert response.status_code == 401
