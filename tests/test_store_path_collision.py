"""Tests for store-namespace path collision detection."""

import io
import uuid

import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.protocols.storage import BundlePathConflictError


@pytest.fixture
def backend(storage_dir: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_dir)


def _meta(files: list[tuple[str, int, str]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "bundle_id": "tid",
        "sender": None,
        "created_at": "2026-04-18T16:00:00+00:00",
        "content_type": "application/octet-stream",
        "file_count": len(files),
        "files": [{"name": n, "size": s, "mime": m} for n, s, m in files],
    }


async def _commit(backend: FilesystemBackend, path: str, files: list[tuple[str, bytes]]) -> None:
    writer = await backend.open_bundle_write(path)
    for name, data in files:
        await writer.write_file(name, io.BytesIO(data))
    meta = _meta([(n, len(d), "application/octet-stream") for n, d in files])
    await writer.commit(meta)


class TestShadowChild:
    async def test_committed_parent_blocks_child(self, backend: FilesystemBackend) -> None:
        await _commit(backend, "store/proj", [("readme.md", b"hi")])

        with pytest.raises(BundlePathConflictError) as excinfo:
            await backend.open_bundle_write("store/proj/nested")
        assert excinfo.value.kind == "shadow_child"
        assert excinfo.value.conflicting_path == "store/proj"


class TestShadowParent:
    async def test_committed_child_blocks_parent(self, backend: FilesystemBackend) -> None:
        await _commit(backend, "store/proj/sub/doc", [("f.md", b"x")])

        with pytest.raises(BundlePathConflictError) as excinfo:
            await backend.open_bundle_write("store/proj/sub")
        assert excinfo.value.kind == "shadow_parent"
        assert "store/proj/sub/doc" in excinfo.value.conflicting_path


class TestInFlightShadowChild:
    async def test_partial_bundle_still_blocks_child(self, backend: FilesystemBackend) -> None:
        writer = await backend.open_bundle_write("store/proj")
        await writer.write_file("f.txt", io.BytesIO(b"partial"))
        # No commit — partial bundle dir exists without meta.json

        with pytest.raises(BundlePathConflictError) as excinfo:
            await backend.open_bundle_write("store/proj/nested")
        assert excinfo.value.kind == "shadow_child"


class TestConcurrentMkdir:
    async def test_second_write_to_same_path_fails(self, backend: FilesystemBackend) -> None:
        await backend.open_bundle_write("store/racy")
        with pytest.raises(BundlePathConflictError):
            await backend.open_bundle_write("store/racy")


class TestInboxDoesNotCollide:
    async def test_inbox_uuid_namespace_never_collides(self, backend: FilesystemBackend) -> None:
        # Inbox path is {recipient}/{bundle_id}. Two UUIDs never collide.
        a = uuid.uuid4().hex
        b = uuid.uuid4().hex
        await backend.open_bundle_write(f"inbox/alice/{a}")
        await backend.open_bundle_write(f"inbox/alice/{b}")
