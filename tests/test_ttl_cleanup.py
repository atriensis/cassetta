import asyncio
from unittest.mock import patch

from cassetta.app import _ttl_cleanup_loop
from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.config import AppConfig

# Save real sleep before any patching
_real_sleep = asyncio.sleep


# 36 bytes, above the 32 `config.py` requires of an HS256 key. See `tests/test_signing_key_fixtures.py`.
_TEST_JWT_KEY = b"test-test-test-test-test-test-test-t"


def _make_config(**overrides: object) -> AppConfig:
    defaults = {
        "setup_token": "test",
        "dev_mode": True,
        "storage_path": "/tmp/test",
        "keys_file": "/tmp/keys.json",
        "default_ttl": 3600,
        "allowed_path_chars": r"a-zA-Z0-9\-_./",
        "mcp_allowed_hosts": (),
    }
    defaults.update(overrides)
    # The signing key is lifted out of the dict and named at the call, so the guard can see it. A key
    # inside `**defaults` is a field list assembled somewhere else, which is exactly the shape that
    # lost `jwt_primary_key` in the first place. `pop` keeps a caller's override working.
    jwt_primary_key = defaults.pop("jwt_primary_key", _TEST_JWT_KEY)
    return AppConfig(jwt_primary_key=jwt_primary_key, **defaults)  # type: ignore[arg-type]


async def _fast_sleep(_seconds: float) -> None:
    """Near-instant sleep replacement for tests."""
    await _real_sleep(0.01)


class TestCleanupAcquiresLease:
    @patch("cassetta.app.asyncio.sleep", side_effect=_fast_sleep)
    async def test_cleanup_acquires_and_releases_lease(self, mock_sleep: object, storage_dir: str) -> None:
        backend = FilesystemBackend(root_path=storage_dir)
        config = _make_config(storage_path=storage_dir, default_ttl=3600)

        acquired = asyncio.Event()
        released = asyncio.Event()

        original_acquire = backend.acquire_lease
        original_release = backend.release_lease

        async def tracking_acquire(key: str, ttl_seconds: int = 60) -> str | None:
            result = await original_acquire(key, ttl_seconds)
            if result is not None:
                acquired.set()
            return result

        async def tracking_release(key: str, lease_id: str) -> bool:
            result = await original_release(key, lease_id)
            if result:
                released.set()
            return result

        backend.acquire_lease = tracking_acquire  # type: ignore[assignment]
        backend.release_lease = tracking_release  # type: ignore[assignment]

        task = asyncio.create_task(_ttl_cleanup_loop(backend, config))
        try:
            await asyncio.wait_for(acquired.wait(), timeout=5.0)
            await asyncio.wait_for(released.wait(), timeout=5.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @patch("cassetta.app.asyncio.sleep", side_effect=_fast_sleep)
    async def test_cleanup_skips_if_lease_held(self, mock_sleep: object, storage_dir: str) -> None:
        backend = FilesystemBackend(root_path=storage_dir)
        config = _make_config(storage_path=storage_dir, default_ttl=3600)

        # Pre-acquire the lease
        lease_id = await backend.acquire_lease("ttl-cleanup", ttl_seconds=60)
        assert lease_id is not None

        sweep_ran = False
        skip_count = 0

        original_acquire = backend.acquire_lease

        async def tracking_acquire(key: str, ttl_seconds: int = 60) -> str | None:
            nonlocal skip_count
            result = await original_acquire(key, ttl_seconds)
            if result is None:
                skip_count += 1
            return result

        original_list = backend.list

        async def tracking_list(prefix: str = "") -> list[str]:
            nonlocal sweep_ran
            sweep_ran = True
            return await original_list(prefix)

        backend.acquire_lease = tracking_acquire  # type: ignore[assignment]
        backend.list = tracking_list  # type: ignore[assignment]

        task = asyncio.create_task(_ttl_cleanup_loop(backend, config))
        for _ in range(100):
            if skip_count > 0:
                break
            await _real_sleep(0.02)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert not sweep_ran, "Cleanup should not run when lease is held"
        assert skip_count > 0, "Cleanup should have attempted and skipped"

        await backend.release_lease("ttl-cleanup", lease_id)

    @patch("cassetta.app.asyncio.sleep", side_effect=_fast_sleep)
    async def test_cleanup_handles_no_ttl(self, mock_sleep: object, storage_dir: str) -> None:
        backend = FilesystemBackend(root_path=storage_dir)
        config = _make_config(storage_path=storage_dir, default_ttl=0)

        acquire_called = False
        original_acquire = backend.acquire_lease

        async def tracking_acquire(key: str, ttl_seconds: int = 60) -> str | None:
            nonlocal acquire_called
            acquire_called = True
            return await original_acquire(key, ttl_seconds)

        backend.acquire_lease = tracking_acquire  # type: ignore[assignment]

        cycle_count = 0

        async def counting_sleep(_seconds: float) -> None:
            nonlocal cycle_count
            cycle_count += 1
            await _real_sleep(0.01)

        with patch("cassetta.app.asyncio.sleep", side_effect=counting_sleep):
            task = asyncio.create_task(_ttl_cleanup_loop(backend, config))
            for _ in range(100):
                if cycle_count >= 3:
                    break
                await _real_sleep(0.02)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert not acquire_called, "Should not acquire lease when TTL is 0"
