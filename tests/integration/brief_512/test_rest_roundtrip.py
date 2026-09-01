"""Brief 512 US3 — REST round-trip send/pick/get/delete on the new layout."""

import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.mime import pick_mime


async def seed_inbox_bundle(
    backend, recipient: str, path: str,
    *, content: bytes | None = None, sender: str | None = None,
) -> str:
    bundle_path = f"inbox/{recipient}/{path}"
    bundle_id = uuid.uuid4().hex
    file_parts = [(path.split("/")[-1], content or b"")]
    writer = await backend.open_bundle_write(bundle_path)
    try:
        records: list[dict[str, Any]] = []
        for name, data in file_parts:
            await writer.write_file(name, io.BytesIO(data))
            records.append({"name": name, "size": len(data), "mime": pick_mime(name, explicit=None)})
        meta: dict[str, Any] = {
            "schema_version": 1, "bundle_id": bundle_id, "sender": sender,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": len(records), "files": records,
        }
        await writer.commit(meta)
        return bundle_id
    except Exception:
        await writer.abort()
        raise


@pytest.fixture
def backend(storage_dir: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_dir)


@pytest.fixture
async def rest_client(
    client: httpx.AsyncClient,
) -> AsyncIterator[httpx.AsyncClient]:
    """Wrap the shared client fixture but pre-register the ``dev:agent`` key."""
    # Register a dev:agent key so DefaultAliasResolver resolves it.
    resp = await client.post(
        "/keys",
        json={"host": "dev", "project": "agent"},
    )
    assert resp.status_code in {200, 201}, resp.text
    yield client


class TestInboxRoundtrip:
    async def test_put_list_get_pick_delete(
        self, rest_client: httpx.AsyncClient, backend: FilesystemBackend,
    ) -> None:
        client = rest_client
        # Seed directly — legacy PUT is gone in Brief 514.
        transport = client._transport  # type: ignore[attr-defined]
        live_backend = transport.app.state.backends.backend  # type: ignore[attr-defined]
        bundle_id = await seed_inbox_bundle(
            live_backend, "dev:agent", "note.md",
            content=b"# First note", sender="dev:agent",
        )
        assert bundle_id

        list_resp = await client.get("/inbox/dev:agent/")
        listing = list_resp.json()["files"]
        assert len(listing) == 1
        entry = listing[0]
        assert entry["path"] == "note.md"
        assert entry["bundle_id"]
        assert entry["file_count"] == 1
        assert entry["files"][0]["name"] == "note.md"

        get_resp = await client.get("/inbox/dev:agent/note.md")
        get_envelope = get_resp.json()
        assert get_envelope["mode"] == "inline"
        assert get_envelope["files"][0]["content"] == "# First note"

        pick_resp = await client.post("/inbox/dev:agent/note.md/pick")
        pick_envelope = pick_resp.json()
        assert pick_envelope["mode"] == "inline"
        assert pick_envelope["files"][0]["content"] == "# First note"

        # Bundle directory removed
        with pytest.raises(FileNotFoundError):
            await live_backend.read_bundle_meta("inbox/dev:agent/note.md")


class TestStoreRoundtrip:
    async def test_put_list_get_delete(
        self, client: httpx.AsyncClient, backend: FilesystemBackend,
    ) -> None:
        put_resp = await client.put("/files/proj/readme.md", content=b"# Hi")
        assert put_resp.status_code == 201

        listing = (await client.get("/files/")).json()["files"]
        paths = {item["path"]: item for item in listing}
        assert "proj/readme.md" in paths
        assert paths["proj/readme.md"]["files"][0]["name"] == "readme.md"

        get_resp = await client.get("/files/proj/readme.md")
        get_envelope = get_resp.json()
        assert get_envelope["mode"] == "inline"
        assert get_envelope["files"][0]["content"] == "# Hi"

        await client.delete("/files/proj/readme.md")

        with pytest.raises(FileNotFoundError):
            await backend.read_bundle_meta("store/proj/readme.md")


class TestMultiFileEnvelope:
    async def test_multipart_roundtrip_carries_bundle_envelope(
        self, client: httpx.AsyncClient,
    ) -> None:
        files = [
            ("files", ("plan.md", b"# Plan", "text/markdown")),
            ("files", ("diagram.png", b"PNG-ish", "image/png")),
        ]
        await client.put("/files/pack", files=files)

        resp = await client.get("/files/pack")
        body = resp.json()
        assert isinstance(body["bundle"], dict)
        assert body["bundle"]["schema_version"] == 1
        assert body["bundle"]["file_count"] == 2
        assert {f["name"] for f in body["bundle"]["files"]} == {"plan.md", "diagram.png"}
        assert {f["name"] for f in body["files"]} == {"plan.md", "diagram.png"}


class TestStoreCollision409:
    async def test_put_under_existing_bundle_returns_409(
        self, client: httpx.AsyncClient,
    ) -> None:
        await client.put("/files/proj/readme.md", content=b"# hi")
        resp = await client.put(
            "/files/proj/readme.md/inner", content=b"nested"
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "bundle_path_conflict"
        assert body["kind"] == "shadow_child"
        assert body["conflicting_path"] == "store/proj/readme.md"

    async def test_put_over_existing_parent_bundle_returns_409(
        self, client: httpx.AsyncClient,
    ) -> None:
        await client.put("/files/proj/sub/doc.md", content=b"child")
        resp = await client.put("/files/proj/sub", content=b"parent")
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "bundle_path_conflict"
        assert body["kind"] == "shadow_parent"


class TestDeleteRemovesBundleDir:
    async def test_inbox_delete_removes_directory(
        self, rest_client: httpx.AsyncClient, storage_dir: str,
    ) -> None:
        from pathlib import Path
        client = rest_client
        transport = client._transport  # type: ignore[attr-defined]
        live_backend = transport.app.state.backends.backend  # type: ignore[attr-defined]
        await seed_inbox_bundle(
            live_backend, "dev:agent", "msg.txt",
            content=b"hi", sender="dev:agent",
        )
        bundle_dir = Path(storage_dir) / "data" / "inbox" / "dev:agent" / "msg.txt"
        assert bundle_dir.is_dir()
        response = await client.delete("/inbox/dev:agent/msg.txt")
        assert response.status_code == 204
        assert not bundle_dir.exists()

    async def test_store_delete_removes_directory(
        self, client: httpx.AsyncClient, storage_dir: str,
    ) -> None:
        from pathlib import Path
        await client.put("/files/proj/readme.md", content=b"hi")
        bundle_dir = Path(storage_dir) / "data" / "store" / "proj" / "readme.md"
        assert bundle_dir.is_dir()
        await client.delete("/files/proj/readme.md")
        assert not bundle_dir.exists()


class TestPickLatest:
    async def test_latest_picks_newest_committed_bundle(
        self, rest_client: httpx.AsyncClient, storage_dir: str,
    ) -> None:
        import os
        import time as _time
        from pathlib import Path

        client = rest_client
        transport = client._transport  # type: ignore[attr-defined]
        live_backend = transport.app.state.backends.backend  # type: ignore[attr-defined]
        await seed_inbox_bundle(
            live_backend, "dev:agent", "first.txt",
            content=b"first", sender="dev:agent",
        )
        _time.sleep(0.05)
        await seed_inbox_bundle(
            live_backend, "dev:agent", "second.txt",
            content=b"second", sender="dev:agent",
        )

        # Plant a partial (uncommitted) bundle newer than both — must be ignored.
        later = Path(storage_dir) / "data" / "inbox" / "dev:agent" / "partial.txt"
        os.makedirs(later, exist_ok=True)
        (later / "partial.txt").write_bytes(b"partial")

        response = await client.post("/inbox/dev:agent/latest/pick")
        envelope = response.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "second"
