"""T030 (US3) — legacy raw-body REST read is retired.

Every REST read route (``GET /files/{path}``, ``GET /inbox/{agent}/{path}``,
and REST pick) returns ``Content-Type: application/json`` with the
unified envelope for both single-file AND multi-file bundles. There
is no code path that returns raw bytes with the file's mime type.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend

from .conftest import seed_inbox_bundle, seed_store_bundle


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["files", "inbox"])
async def test_single_and_multi_file_rest_returns_json(
    core_app: tuple[httpx.AsyncClient, str],
    h,
    route: str,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", f"single-{route}")
    alice_key = await h.create_key(client, sender_key, "alice", f"single-{route}")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))

    if route == "files":
        await seed_store_bundle(
            backend,
            f"single-{route}",
            files=[("note.md", b"# Hello\n")],
        )
        url = f"/files/single-{route}"
    else:
        await seed_inbox_bundle(
            backend,
            f"alice:single-{route}",
            "note.md",
            content=b"# Hello\n",
            sender="bob",
        )
        url = f"/inbox/alice:single-{route}/note.md"

    resp = await client.get(
        url,
        headers={"Authorization": f"Bearer {alice_key}"},
    )
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    # Always application/json — never raw bytes with file's mime.
    assert resp.headers["content-type"].startswith("application/json"), (
        f"expected application/json, got {resp.headers['content-type']!r}"
    )
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert len(envelope["files"]) == 1
    assert envelope["files"][0]["name"] == "note.md"
    assert envelope["files"][0]["content"] == "# Hello\n"


@pytest.mark.asyncio
async def test_rest_pick_returns_json_envelope(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """REST pick (POST /inbox/{agent}/{path}/pick) returns JSON, not raw bytes."""
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "pick-json")
    alice_key = await h.create_key(client, sender_key, "alice", "pick-json")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    await seed_inbox_bundle(
        backend,
        "alice:pick-json",
        "single.txt",
        content=b"one file\n",
        sender="bob",
    )

    resp = await client.post(
        "/inbox/alice:pick-json/single.txt/pick",
        headers={"Authorization": f"Bearer {alice_key}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json"), resp.headers["content-type"]
    envelope = resp.json()
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["name"] == "single.txt"
    assert envelope["files"][0]["content"] == "one file\n"


@pytest.mark.asyncio
async def test_legacy_raw_bytes_shape_is_retired(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """Sanity: a raw-body test would FAIL — no route returns text/markdown."""
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "no-raw")
    alice_key = await h.create_key(client, sender_key, "alice", "no-raw")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    await seed_inbox_bundle(
        backend,
        "alice:no-raw",
        "file.md",
        content=b"# Heading\n",
        sender="bob",
    )
    await seed_store_bundle(
        backend,
        "file.md",
        files=[("file.md", b"# Heading\n")],
    )

    for url in (
        "/inbox/alice:no-raw/file.md",
        "/files/file.md",
    ):
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {alice_key}"},
        )
        assert resp.status_code == 200, (resp.status_code, url)
        ct = resp.headers["content-type"]
        # Old shape: text/markdown. New shape: application/json.
        assert "text/markdown" not in ct, f"{url} still returns legacy raw-bytes shape ({ct!r})"
        # Body is JSON-decodable.
        assert json.loads(resp.content)["mode"] == "inline"
