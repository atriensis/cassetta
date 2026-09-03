"""Tests for list_bundles / read_bundle_meta / file reads."""

import io
import json
import logging
from pathlib import Path

import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend


@pytest.fixture
def backend(storage_dir: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_dir)


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


async def _commit(backend: FilesystemBackend, path: str, files: list[tuple[str, bytes]]) -> None:
    writer = await backend.open_bundle_write(path)
    for name, data in files:
        await writer.write_file(name, io.BytesIO(data))
    meta = _valid_meta([(n, len(d), "application/octet-stream") for n, d in files])
    await writer.commit(meta)


class TestListBundles:
    async def test_default_filters_committed_only(self, backend: FilesystemBackend) -> None:
        await _commit(backend, "store/a", [("f.txt", b"12")])
        await _commit(backend, "store/b", [("f.txt", b"123")])
        # Uncommitted bundle: open+write, no commit
        writer = await backend.open_bundle_write("store/pending")
        await writer.write_file("f.txt", io.BytesIO(b"partial"))

        refs = list(backend.list_bundles("store/"))
        paths = {r.path for r in refs}
        assert paths == {"store/a", "store/b"}
        for r in refs:
            assert r.has_meta is True

    async def test_include_orphans_returns_both(self, backend: FilesystemBackend) -> None:
        await _commit(backend, "store/a", [("f.txt", b"12")])
        writer = await backend.open_bundle_write("store/pending")
        await writer.write_file("f.txt", io.BytesIO(b"partial"))

        refs = list(backend.list_bundles("store/", include_orphans=True))
        paths = {r.path: r.has_meta for r in refs}
        assert paths == {"store/a": True, "store/pending": False}

    async def test_stray_non_directory_skipped_with_warning(
        self,
        backend: FilesystemBackend,
        storage_dir: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _commit(backend, "store/a", [("f.txt", b"12")])
        # Drop a stray flat file under store/ (non-bundle)
        stray = Path(storage_dir) / "data" / "store" / "stray.dat"
        stray.write_bytes(b"not a bundle dir")

        with caplog.at_level(logging.WARNING, logger="cassetta"):
            refs = list(backend.list_bundles("store/"))
        paths = {r.path for r in refs}
        assert paths == {"store/a"}

    async def test_mtime_populated(self, backend: FilesystemBackend) -> None:
        await _commit(backend, "store/x", [("f.txt", b"1")])
        refs = list(backend.list_bundles("store/"))
        assert refs[0].mtime > 0


class TestReadBundleMeta:
    async def test_happy_path(self, backend: FilesystemBackend) -> None:
        await _commit(backend, "store/read", [("f.txt", b"abc")])
        meta = await backend.read_bundle_meta("store/read")
        assert meta["schema_version"] == 1
        assert meta["file_count"] == 1

    async def test_missing_directory_raises(self, backend: FilesystemBackend) -> None:
        with pytest.raises(FileNotFoundError):
            await backend.read_bundle_meta("store/missing")

    async def test_partial_bundle_raises(self, backend: FilesystemBackend) -> None:
        writer = await backend.open_bundle_write("store/partial")
        await writer.write_file("f.txt", io.BytesIO(b"x"))
        # No commit
        with pytest.raises(FileNotFoundError):
            await backend.read_bundle_meta("store/partial")


class TestOpenBundleFileRead:
    async def test_reads_inner_file(self, backend: FilesystemBackend) -> None:
        await _commit(
            backend,
            "store/inner",
            [
                ("a.txt", b"first"),
                ("b.txt", b"second"),
            ],
        )
        handle = await backend.open_bundle_file_read("store/inner", "b.txt")
        try:
            assert handle.read() == b"second"
        finally:
            handle.close()

    async def test_missing_file_raises(self, backend: FilesystemBackend) -> None:
        await _commit(backend, "store/inner", [("a.txt", b"x")])
        with pytest.raises(FileNotFoundError):
            await backend.open_bundle_file_read("store/inner", "nope.txt")

    async def test_no_meta_raises(self, backend: FilesystemBackend) -> None:
        writer = await backend.open_bundle_write("store/nometa")
        await writer.write_file("a.txt", io.BytesIO(b"x"))
        with pytest.raises(FileNotFoundError):
            await backend.open_bundle_file_read("store/nometa", "a.txt")


class TestDeleteBundle:
    async def test_deletes_entire_directory(self, backend: FilesystemBackend, storage_dir: str) -> None:
        await _commit(backend, "store/gone", [("f.txt", b"x")])
        await backend.delete_bundle("store/gone")

        bundle_dir = Path(storage_dir) / "data" / "store" / "gone"
        assert not bundle_dir.exists()

    async def test_raises_when_absent(self, backend: FilesystemBackend) -> None:
        with pytest.raises(FileNotFoundError):
            await backend.delete_bundle("store/nope")


class TestMetaOnDisk:
    async def test_meta_json_roundtrip(self, backend: FilesystemBackend, storage_dir: str) -> None:
        await _commit(backend, "store/rt", [("f.txt", b"hi")])
        on_disk = json.loads((Path(storage_dir) / "data" / "store" / "rt" / "meta.json").read_text())
        assert on_disk == await backend.read_bundle_meta("store/rt")
