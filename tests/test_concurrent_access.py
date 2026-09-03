"""Concurrent access integration tests.

Verify that simultaneous operations don't race or corrupt data.

The legacy ``PUT /inbox/{agent}/{path}`` endpoint is gone, so the
former REST-level concurrent PUT tests (dual upload, simultaneous sender
labels) no longer apply — they are covered end-to-end by the new upload
flow tests. What remains here is the backend-level concurrency test.
"""

import asyncio
import io
import uuid
from datetime import UTC
from datetime import datetime as dt

from cassetta.backends.filesystem.storage import FilesystemBackend


class TestConcurrentUploadAndCleanup:
    async def test_upload_survives_concurrent_delete(self, storage_dir: str) -> None:
        """Writing a fresh bundle while another bundle is deleted — both
        operations succeed and the resulting directory state is consistent."""
        backend = FilesystemBackend(root_path=storage_dir)

        async def _seed_bundle(path: str, data: bytes) -> None:
            writer = await backend.open_bundle_write(path)
            try:
                await writer.write_file(path.split("/")[-1], io.BytesIO(data))
                await writer.commit(
                    {
                        "schema_version": 1,
                        "bundle_id": uuid.uuid4().hex,
                        "sender": None,
                        "created_at": dt.now(UTC).isoformat(),
                        "content_type": "application/octet-stream",
                        "file_count": 1,
                        "files": [
                            {
                                "name": path.split("/")[-1],
                                "size": len(data),
                                "mime": "application/octet-stream",
                            }
                        ],
                    }
                )
            except Exception:
                await writer.abort()
                raise

        await _seed_bundle("store/old.txt", b"old data")
        await _seed_bundle("store/keep.txt", b"keep data")

        async def _put_fresh() -> None:
            await _seed_bundle("store/fresh.txt", b"fresh data")

        async def _delete_old() -> None:
            await backend.delete_bundle("store/old.txt")

        await asyncio.gather(_put_fresh(), _delete_old())

        paths = {ref.path for ref in backend.list_bundles("store/")}
        assert "store/keep.txt" in paths
        assert "store/fresh.txt" in paths
        assert "store/old.txt" not in paths
