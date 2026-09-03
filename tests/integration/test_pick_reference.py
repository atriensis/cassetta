"""T009 (US1) — ``cassetta_pick`` returns reference-mode envelope.

Send a 3-file over-threshold bundle to ``alice``, call ``cassetta_pick``
via the MCP test client, assert the envelope shape, assert a claim
sidecar exists after the pick, fetch each URL with the returned
``download_token``, assert bytes match, and assert the bundle
directory is gone after the last GET.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend

from .conftest import seed_inbox_bundle


def _unwrap(result: dict) -> dict | str:
    text = result["content"][0]["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@pytest.mark.asyncio
async def test_pick_returns_reference_envelope_and_completes(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
    tmp_path: Path,
) -> None:
    client, _ = core_app_small_inline
    _ = tmp_path

    sender_key = await h.setup_agent(client, "bob", "main")
    alice_key = await h.create_key(client, sender_key, "alice", "main")

    # Seed a 3-file bundle above the 32-byte inline cap directly on the
    # backend — we intentionally bypass policy caps on ingest because the
    # point of this test is the READ path.
    backend = FilesystemBackend(
        root_path=_storage_path_of(client),
    )
    files = [
        ("README.md", b"# Hello\n" + b"a" * 100),
        ("src/main.py", b"print('brief 515 integration')\n" * 10),
        ("docs/arch.md", b"## Architecture\n" + b"b" * 150),
    ]
    await seed_inbox_bundle(
        backend,
        "alice:main",
        "project-docs",
        files=files,
        sender="bob",
    )

    sid = await h.mcp_init(client, api_key=alice_key)
    pick = await h.mcp_call(
        client,
        "cassetta_pick",
        {"path": "project-docs"},
        sid=sid,
        api_key=alice_key,
    )
    envelope = _unwrap(pick)
    assert isinstance(envelope, dict), f"expected JSON envelope, got: {envelope!r}"
    assert envelope["mode"] == "reference", envelope
    assert "bundle" in envelope
    assert envelope["bundle"]["file_count"] == 3
    assert "download_token" in envelope and envelope["download_token"]
    assert "expires_at" in envelope
    assert isinstance(envelope["files"], list) and len(envelope["files"]) == 3
    for file_entry in envelope["files"]:
        assert "url" in file_entry
        assert "name" in file_entry
        assert "size" in file_entry
        assert "mime" in file_entry
        assert "content" not in file_entry, f"reference envelope MUST NOT carry content: {file_entry!r}"

    # Claim sidecar is on disk under data/.claims/.
    storage_root = Path(_storage_path_of(client))
    claims_dir = storage_root / ".claims"
    assert claims_dir.is_dir(), f"claims dir not created: {claims_dir}"
    sidecars = list(claims_dir.glob("*.json"))
    assert len(sidecars) == 1, f"expected 1 sidecar, got {sidecars!r}"

    # Fetch each URL with Authorization: Bearer <download_token>.
    # The download endpoint also requires identity — we supply X-Sender
    # matching claim.recipient ("alice:main").
    token = envelope["download_token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Sender": "alice:main",
    }
    fetched: dict[str, bytes] = {}
    for file_entry in envelope["files"]:
        url_path = urllib.parse.urlparse(file_entry["url"]).path
        resp = await client.get(url_path, headers=headers)
        assert resp.status_code == 200, f"expected 200 for {file_entry['name']}; got {resp.status_code}: {resp.text}"
        fetched[file_entry["name"]] = resp.content

    # Bytes match sender's originals.
    for name, data in files:
        assert fetched[name] == data, f"byte-mismatch for {name}"

    # Bundle is gone after last GET — a follow-up pick MUST raise
    # "File not found".
    follow = await h.mcp_call(
        client,
        "cassetta_pick",
        {"path": "project-docs"},
        sid=sid,
        api_key=alice_key,
    )
    # MCP tool errors come back as `isError=True` content items.
    assert follow.get("isError") is True, follow


def _storage_path_of(client: httpx.AsyncClient) -> str:
    import os

    return os.environ["CASSETTA_STORAGE_PATH"]
