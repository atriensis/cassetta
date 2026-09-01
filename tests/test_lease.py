import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend


@pytest.fixture
def backend(storage_dir: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_dir)


class TestAcquireLease:
    async def test_acquire_returns_lease_id(self, backend: FilesystemBackend) -> None:
        lease_id = await backend.acquire_lease("test-lock", ttl_seconds=60)
        assert lease_id is not None
        assert isinstance(lease_id, str)
        assert len(lease_id) > 0

    async def test_double_acquire_returns_none(self, backend: FilesystemBackend) -> None:
        lease_id = await backend.acquire_lease("test-lock", ttl_seconds=60)
        assert lease_id is not None
        second = await backend.acquire_lease("test-lock", ttl_seconds=60)
        assert second is None

    async def test_different_keys_independent(self, backend: FilesystemBackend) -> None:
        id1 = await backend.acquire_lease("lock-a", ttl_seconds=60)
        id2 = await backend.acquire_lease("lock-b", ttl_seconds=60)
        assert id1 is not None
        assert id2 is not None


class TestRenewLease:
    async def test_renew_active_lease(self, backend: FilesystemBackend) -> None:
        lease_id = await backend.acquire_lease("test-lock", ttl_seconds=60)
        assert lease_id is not None
        result = await backend.renew_lease("test-lock", lease_id)
        assert result is True

    async def test_renew_wrong_id_fails(self, backend: FilesystemBackend) -> None:
        lease_id = await backend.acquire_lease("test-lock", ttl_seconds=60)
        assert lease_id is not None
        result = await backend.renew_lease("test-lock", "wrong-id")
        assert result is False

    async def test_renew_unheld_key_fails(self, backend: FilesystemBackend) -> None:
        result = await backend.renew_lease("no-such-lock", "any-id")
        assert result is False


class TestReleaseLease:
    async def test_release_active_lease(self, backend: FilesystemBackend) -> None:
        lease_id = await backend.acquire_lease("test-lock", ttl_seconds=60)
        assert lease_id is not None
        result = await backend.release_lease("test-lock", lease_id)
        assert result is True

    async def test_release_wrong_id_fails(self, backend: FilesystemBackend) -> None:
        lease_id = await backend.acquire_lease("test-lock", ttl_seconds=60)
        assert lease_id is not None
        result = await backend.release_lease("test-lock", "wrong-id")
        assert result is False

    async def test_release_allows_reacquire(self, backend: FilesystemBackend) -> None:
        lease_id = await backend.acquire_lease("test-lock", ttl_seconds=60)
        assert lease_id is not None
        await backend.release_lease("test-lock", lease_id)
        new_id = await backend.acquire_lease("test-lock", ttl_seconds=60)
        assert new_id is not None
        assert new_id != lease_id

    async def test_release_unheld_key_fails(self, backend: FilesystemBackend) -> None:
        result = await backend.release_lease("no-such-lock", "any-id")
        assert result is False


class TestLeaseDoesNotAffectList:
    async def test_locks_not_visible_in_list(self, backend: FilesystemBackend) -> None:
        await backend.put("user-file.txt", b"data")
        await backend.acquire_lease("my-lock", ttl_seconds=60)
        files = await backend.list()
        assert files == ["user-file.txt"]
        assert not any("locks" in f for f in files)
