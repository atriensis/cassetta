"""Brief 512 US3 — multicast fan-out produces distinct bundle_ids per recipient."""

from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend


@pytest.fixture
def backend(storage_dir: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_dir)


@pytest.fixture
async def bcast_client(
    client: httpx.AsyncClient,
) -> AsyncIterator[httpx.AsyncClient]:
    await client.post("/keys", json={"host": "alice", "project": "proj"})
    await client.post("/keys", json={"host": "bob", "project": "proj"})
    yield client


class TestBroadcastDistinctBundleIds:
    async def test_broadcast_bundle_ids_differ_per_recipient(
        self,
        bcast_client: httpx.AsyncClient,
        backend: FilesystemBackend,
    ) -> None:
        files = [
            ("files", ("update.md", b"# Update", "text/markdown")),
            ("files", ("data.csv", b"a,b,c", "text/csv")),
        ]
        resp = await bcast_client.post(
            "/broadcast/team-update",
            files=files,
        )
        assert resp.status_code == 200

        meta_alice = await backend.read_bundle_meta("inbox/alice:proj/team-update")
        meta_bob = await backend.read_bundle_meta("inbox/bob:proj/team-update")
        assert meta_alice["bundle_id"] != meta_bob["bundle_id"]
        assert meta_alice["file_count"] == 2
        assert meta_bob["file_count"] == 2
