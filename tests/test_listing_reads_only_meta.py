"""Brief 512 SC-005 audit — listing never reads file bytes.

Listings must be answerable from ``meta.json`` alone. A spy wraps
``FilesystemBackend.open_bundle_file_read`` and asserts it is never
invoked during REST listing calls.
"""

from __future__ import annotations

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend

from .conftest import seed_inbox_bundle


class _ReadSpy:
    def __init__(self, backend: FilesystemBackend) -> None:
        self._original = backend.open_bundle_file_read
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, bundle_path: str, file_name: str):
        self.calls.append((bundle_path, file_name))
        return await self._original(bundle_path, file_name)


@pytest.fixture
async def spied_client(
    client: httpx.AsyncClient,
) -> tuple[httpx.AsyncClient, _ReadSpy]:
    # client fixture exposes the backend on app.state via ASGI transport.
    transport = client._transport  # type: ignore[attr-defined]
    app = transport.app  # type: ignore[attr-defined]
    backend: FilesystemBackend = app.state.backends.backend
    spy = _ReadSpy(backend)
    backend.open_bundle_file_read = spy  # type: ignore[method-assign]
    # Seed a dev:agent key so inbox listings resolve.
    resp = await client.post("/keys", json={"host": "dev", "project": "agent"})
    assert resp.status_code in {200, 201}, resp.text
    return client, spy


class TestListingReadsOnlyMeta:
    async def test_store_listing_never_reads_file_bytes(
        self,
        spied_client: tuple[httpx.AsyncClient, _ReadSpy],
    ) -> None:
        client, spy = spied_client
        await client.put("/files/a.md", content=b"# a")
        await client.put("/files/nested/b.png", content=b"PNGDATA")
        assert spy.calls == []  # sanity: PUT doesn't read

        response = await client.get("/files/")
        assert response.status_code == 200
        assert len(response.json()["files"]) == 2
        assert spy.calls == [], f"Store listing read file bytes: {spy.calls}"

    async def test_inbox_listing_never_reads_file_bytes(
        self,
        spied_client: tuple[httpx.AsyncClient, _ReadSpy],
    ) -> None:
        client, spy = spied_client
        transport = client._transport  # type: ignore[attr-defined]
        backend: FilesystemBackend = transport.app.state.backends.backend  # type: ignore[attr-defined]
        await seed_inbox_bundle(
            backend,
            "dev:agent",
            "note.md",
            content=b"# note",
        )
        await seed_inbox_bundle(
            backend,
            "dev:agent",
            "pack",
            files=[("x.md", b"# x"), ("y.md", b"# y")],
        )
        spy.calls.clear()

        response = await client.get("/inbox/dev:agent/")
        assert response.status_code == 200
        assert len(response.json()["files"]) == 2
        assert spy.calls == [], f"Inbox listing read file bytes: {spy.calls}"
