"""Brief 512 US2 scenario — atomic visibility gated on meta.json.

A bundle is visible iff its ``meta.json`` sidecar is present. Directories
with partial writes (no ``meta.json``) are invisible to every listing path
and to direct-read endpoints.
"""

import io
import os
from pathlib import Path

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend


@pytest.fixture
def backend(storage_dir: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_dir)


def _seed_partial_bundle(storage_dir: str, bundle_rel_path: str, content: bytes) -> Path:
    """Write files into a bundle directory without a ``meta.json`` sidecar."""
    bundle_dir = Path(storage_dir) / "data" / bundle_rel_path
    bundle_dir.mkdir(parents=True, exist_ok=True)
    inner = bundle_dir / bundle_rel_path.split("/")[-1]
    inner.write_bytes(content)
    return bundle_dir


class TestPartialBundleInvisible:
    async def test_partial_inbox_bundle_hidden_from_rest_listing(
        self, client: httpx.AsyncClient, storage_dir: str
    ) -> None:
        _seed_partial_bundle(storage_dir, "inbox/alice/handwritten.txt", b"partial")
        response = await client.get("/inbox/alice/")
        assert response.status_code == 200
        assert response.json()["files"] == []

    async def test_partial_store_bundle_hidden_from_rest_listing(
        self, client: httpx.AsyncClient, storage_dir: str
    ) -> None:
        _seed_partial_bundle(storage_dir, "store/handwritten.txt", b"partial")
        response = await client.get("/files/")
        assert response.status_code == 200
        assert response.json()["files"] == []

    async def test_meta_json_unlocks_visibility(
        self,
        client: httpx.AsyncClient,
        storage_dir: str,
        backend: FilesystemBackend,
    ) -> None:
        # Seed a partial inbox bundle; confirm invisible
        bundle_dir = _seed_partial_bundle(
            storage_dir, "inbox/alice/notes.txt", b"hidden"
        )
        response = await client.get("/inbox/alice/")
        assert response.json()["files"] == []

        # Drop a valid meta.json; bundle becomes visible
        meta = (
            '{"schema_version": 1, "bundle_id": "handcrafted", '
            '"sender": null, "created_at": "2026-04-18T16:00:00+00:00", '
            '"content_type": "application/octet-stream", '
            '"file_count": 1, '
            '"files": [{"name": "notes.txt", "size": 6, "mime": "text/plain"}]}'
        )
        (bundle_dir / "meta.json").write_text(meta)

        response = await client.get("/inbox/alice/")
        listing = response.json()["files"]
        assert len(listing) == 1
        assert listing[0]["path"] == "notes.txt"
        assert listing[0]["bundle_id"] == "handcrafted"
        assert listing[0]["files"][0] == {
            "name": "notes.txt", "size": 6, "mime": "text/plain",
        }


class TestInflightCollision:
    async def test_partial_bundle_blocks_shadow_child(
        self, client: httpx.AsyncClient, storage_dir: str
    ) -> None:
        """An uncommitted bundle at ``store/proj/`` blocks PUTs at ``store/proj/X``."""
        os.makedirs(
            os.path.join(storage_dir, "data", "store", "proj"), exist_ok=True
        )
        # Plant a partial-bundle marker (file directly in proj/ with no meta)
        Path(
            os.path.join(storage_dir, "data", "store", "proj", "draft.txt")
        ).write_bytes(b"draft")

        response = await client.put(
            "/files/proj/readme.md", content=b"# Readme"
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "bundle_path_conflict"
        assert body["kind"] == "shadow_child"


class TestCommitInvariant:
    async def test_abort_cleans_bundle_dir(
        self, backend: FilesystemBackend, storage_dir: str
    ) -> None:
        writer = await backend.open_bundle_write("store/doomed")
        await writer.write_file("a.txt", io.BytesIO(b"partial"))
        await writer.abort()

        bundle_dir = Path(storage_dir) / "data" / "store" / "doomed"
        assert not bundle_dir.exists()
