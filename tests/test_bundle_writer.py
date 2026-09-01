"""Tests for FilesystemBundleWriter (Brief 512)."""

import io
import json
from pathlib import Path

import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend


@pytest.fixture
def backend(storage_dir: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_dir)


def _data_root(storage_dir: str) -> Path:
    return Path(storage_dir) / "data"


class TestHappyPath:
    async def test_open_write_commit_two_files(self, backend: FilesystemBackend, storage_dir: str) -> None:
        writer = await backend.open_bundle_write("store/proj")

        await writer.write_file("readme.md", io.BytesIO(b"# Hello"))
        await writer.write_file("notes.txt", io.BytesIO(b"notes"))

        meta = {
            "schema_version": 1,
            "bundle_id": "abc123",
            "sender": "alice",
            "created_at": "2026-04-18T16:00:00+00:00",
            "content_type": "application/octet-stream",
            "file_count": 2,
            "files": [
                {"name": "readme.md", "size": 7, "mime": "text/markdown"},
                {"name": "notes.txt", "size": 5, "mime": "text/plain"},
            ],
        }
        await writer.commit(meta)

        bundle_dir = _data_root(storage_dir) / "store/proj"
        assert (bundle_dir / "meta.json").is_file()
        assert (bundle_dir / "readme.md").read_bytes() == b"# Hello"
        assert (bundle_dir / "notes.txt").read_bytes() == b"notes"
        on_disk = json.loads((bundle_dir / "meta.json").read_text())
        assert on_disk["schema_version"] == 1
        assert on_disk["bundle_id"] == "abc123"

    async def test_meta_json_absent_before_commit(self, backend: FilesystemBackend, storage_dir: str) -> None:
        writer = await backend.open_bundle_write("store/pending")
        await writer.write_file("a.txt", io.BytesIO(b"a"))

        bundle_dir = _data_root(storage_dir) / "store/pending"
        assert bundle_dir.is_dir()
        assert not (bundle_dir / "meta.json").exists()

    async def test_meta_is_written_last(self, backend: FilesystemBackend, storage_dir: str) -> None:
        writer = await backend.open_bundle_write("store/order")
        await writer.write_file("a.txt", io.BytesIO(b"aaa"))
        await writer.write_file("b.txt", io.BytesIO(b"bbbb"))

        meta = _valid_meta(
            [
                ("a.txt", 3, "text/plain"),
                ("b.txt", 4, "text/plain"),
            ]
        )
        await writer.commit(meta)

        bundle_dir = _data_root(storage_dir) / "store/order"
        a_mtime = (bundle_dir / "a.txt").stat().st_mtime
        b_mtime = (bundle_dir / "b.txt").stat().st_mtime
        m_mtime = (bundle_dir / "meta.json").stat().st_mtime
        assert m_mtime >= a_mtime
        assert m_mtime >= b_mtime


class TestAbort:
    async def test_abort_removes_directory(self, backend: FilesystemBackend, storage_dir: str) -> None:
        writer = await backend.open_bundle_write("store/doomed")
        await writer.write_file("x.txt", io.BytesIO(b"xxx"))
        await writer.abort()

        bundle_dir = _data_root(storage_dir) / "store/doomed"
        assert not bundle_dir.exists()

    async def test_abort_is_idempotent(self, backend: FilesystemBackend) -> None:
        writer = await backend.open_bundle_write("store/idem")
        await writer.abort()
        await writer.abort()  # must not raise


class TestCrashed:
    async def test_uncommitted_bundle_not_in_default_listing(self, backend: FilesystemBackend) -> None:
        writer = await backend.open_bundle_write("store/crashed")
        await writer.write_file("x.txt", io.BytesIO(b"x"))
        # Simulate crash: no commit, no abort. The directory and file
        # remain, but meta.json is absent.

        refs = list(backend.list_bundles("store/"))
        assert all(ref.path != "store/crashed" for ref in refs)

    async def test_uncommitted_bundle_visible_with_include_orphans(self, backend: FilesystemBackend) -> None:
        writer = await backend.open_bundle_write("store/crashed2")
        await writer.write_file("x.txt", io.BytesIO(b"x"))

        refs = list(backend.list_bundles("store/", include_orphans=True))
        matching = [r for r in refs if r.path == "store/crashed2"]
        assert len(matching) == 1
        assert matching[0].has_meta is False


class TestValidation:
    async def test_empty_bundle_rejected_at_commit(self, backend: FilesystemBackend) -> None:
        writer = await backend.open_bundle_write("store/empty")
        meta = {
            "schema_version": 1,
            "bundle_id": "b",
            "sender": None,
            "created_at": "2026-04-18T16:00:00+00:00",
            "content_type": "application/octet-stream",
            "file_count": 0,
            "files": [],
        }
        with pytest.raises(ValueError):
            await writer.commit(meta)
        await writer.abort()

    async def test_duplicate_filename_rejected_at_write(self, backend: FilesystemBackend) -> None:
        writer = await backend.open_bundle_write("store/dup")
        await writer.write_file("same.txt", io.BytesIO(b"aaa"))
        with pytest.raises(ValueError):
            await writer.write_file("same.txt", io.BytesIO(b"bbb"))
        await writer.abort()

    async def test_size_mismatch_rejected_at_commit(self, backend: FilesystemBackend) -> None:
        writer = await backend.open_bundle_write("store/sizes")
        await writer.write_file("x.txt", io.BytesIO(b"12345"))  # 5 bytes

        meta = {
            "schema_version": 1,
            "bundle_id": "b",
            "sender": None,
            "created_at": "2026-04-18T16:00:00+00:00",
            "content_type": "application/octet-stream",
            "file_count": 1,
            "files": [
                {"name": "x.txt", "size": 999, "mime": "text/plain"},
            ],
        }
        with pytest.raises(ValueError):
            await writer.commit(meta)
        await writer.abort()

    async def test_schema_version_present(self, backend: FilesystemBackend, storage_dir: str) -> None:
        writer = await backend.open_bundle_write("store/schemaver")
        await writer.write_file("n.txt", io.BytesIO(b"abc"))
        meta = _valid_meta([("n.txt", 3, "text/plain")])
        await writer.commit(meta)

        bundle_dir = _data_root(storage_dir) / "store/schemaver"
        on_disk = json.loads((bundle_dir / "meta.json").read_text())
        assert on_disk["schema_version"] == 1


def _valid_meta(files: list[tuple[str, int, str]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "bundle_id": "tid",
        "sender": None,
        "created_at": "2026-04-18T16:00:00+00:00",
        "content_type": "application/octet-stream",
        "file_count": len(files),
        "files": [{"name": n, "size": s, "mime": m} for n, s, m in files],
    }
