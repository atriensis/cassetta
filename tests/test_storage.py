import time

import pytest


@pytest.fixture
def backend(storage_dir: str):  # noqa: ANN201
    from cassetta.backends.filesystem.storage import FilesystemBackend

    return FilesystemBackend(root_path=storage_dir)


class TestPutAndGet:
    async def test_put_and_get(self, backend) -> None:
        await backend.put("hello.txt", b"Hello, World!")
        data = await backend.get("hello.txt")
        assert data == b"Hello, World!"

    async def test_put_overwrites(self, backend) -> None:
        await backend.put("file.txt", b"v1")
        await backend.put("file.txt", b"v2")
        data = await backend.get("file.txt")
        assert data == b"v2"

    async def test_get_missing_raises(self, backend) -> None:
        with pytest.raises(FileNotFoundError):
            await backend.get("missing.txt")

    async def test_put_creates_nested_dirs(self, backend) -> None:
        await backend.put("a/b/c/deep.txt", b"nested")
        data = await backend.get("a/b/c/deep.txt")
        assert data == b"nested"


class TestDelete:
    async def test_delete_removes_file(self, backend) -> None:
        await backend.put("temp.txt", b"data")
        await backend.delete("temp.txt")
        assert not await backend.exists("temp.txt")

    async def test_delete_missing_raises(self, backend) -> None:
        with pytest.raises(FileNotFoundError):
            await backend.delete("missing.txt")


class TestList:
    async def test_list_empty(self, backend) -> None:
        result = await backend.list()
        assert result == []

    async def test_list_all_files(self, backend) -> None:
        await backend.put("a.txt", b"a")
        await backend.put("b.txt", b"b")
        result = await backend.list()
        assert sorted(result) == ["a.txt", "b.txt"]

    async def test_list_with_prefix(self, backend) -> None:
        await backend.put("data/a.csv", b"a")
        await backend.put("data/b.csv", b"b")
        await backend.put("logs/error.log", b"err")
        result = await backend.list(prefix="data/")
        assert sorted(result) == ["data/a.csv", "data/b.csv"]

    async def test_list_nested(self, backend) -> None:
        await backend.put("a/b/c.txt", b"deep")
        result = await backend.list()
        assert result == ["a/b/c.txt"]


class TestExists:
    async def test_exists_true(self, backend) -> None:
        await backend.put("file.txt", b"data")
        assert await backend.exists("file.txt") is True

    async def test_exists_false(self, backend) -> None:
        assert await backend.exists("missing.txt") is False


class TestCreationTime:
    async def test_get_creation_time(self, backend) -> None:
        before = time.time()
        await backend.put("timed.txt", b"data")
        after = time.time()
        ctime = await backend.get_creation_time("timed.txt")
        assert before <= ctime <= after

    async def test_get_creation_time_missing_raises(self, backend) -> None:
        with pytest.raises(FileNotFoundError):
            await backend.get_creation_time("missing.txt")


class TestFileSize:
    async def test_get_size(self, backend) -> None:
        await backend.put("sized.txt", b"12345")
        size = await backend.get_size("sized.txt")
        assert size == 5
