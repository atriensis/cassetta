"""Tests for storage namespace isolation.

Verify that user-facing operations never expose keys or locks,
even through crafted path prefixes.
"""

import os

import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend


@pytest.fixture
def backend(storage_dir: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_dir)


class TestFilesystemNamespaceIsolation:
    async def test_list_only_returns_data_files(self, backend: FilesystemBackend, storage_dir: str) -> None:
        """list() returns only files from data/ namespace, not keys/ or locks/."""
        await backend.put("user-file.txt", b"user data")

        # Plant files directly in keys/ and locks/ namespaces
        keys_dir = os.path.join(storage_dir, "keys")
        locks_dir = os.path.join(storage_dir, "locks")
        os.makedirs(keys_dir, exist_ok=True)
        os.makedirs(locks_dir, exist_ok=True)

        with open(os.path.join(keys_dir, ".cassetta-keys.json"), "w") as f:
            f.write('{"keys": []}')
        with open(os.path.join(locks_dir, "some-lock"), "w") as f:
            f.write("lock")

        files = await backend.list()
        assert files == ["user-file.txt"]
        assert not any("keys" in f for f in files)
        assert not any("locks" in f for f in files)

    async def test_put_keys_path_lands_in_data(self, backend: FilesystemBackend, storage_dir: str) -> None:
        """User putting 'keys/evil.json' should land in data/keys/evil.json,
        not in the actual keys namespace."""
        await backend.put("keys/evil.json", b"evil data")

        # File should be in data/keys/evil.json
        data_path = os.path.join(storage_dir, "data", "keys", "evil.json")
        assert os.path.exists(data_path)

        # NOT in the real keys namespace
        real_keys_path = os.path.join(storage_dir, "keys", "evil.json")
        assert not os.path.exists(real_keys_path)

        # Accessible via get
        data = await backend.get("keys/evil.json")
        assert data == b"evil data"

        # Visible in list
        files = await backend.list()
        assert "keys/evil.json" in files

    async def test_put_locks_path_lands_in_data(self, backend: FilesystemBackend, storage_dir: str) -> None:
        """User putting 'locks/evil' should land in data/locks/evil."""
        await backend.put("locks/evil", b"evil data")

        data_path = os.path.join(storage_dir, "data", "locks", "evil")
        assert os.path.exists(data_path)

        real_locks_path = os.path.join(storage_dir, "locks", "evil")
        assert not os.path.exists(real_locks_path)

    async def test_lease_files_not_in_list(self, backend: FilesystemBackend) -> None:
        """Active leases don't appear in user-facing list."""
        await backend.put("user-file.txt", b"data")
        await backend.acquire_lease("my-lock", ttl_seconds=60)
        files = await backend.list()
        assert files == ["user-file.txt"]
