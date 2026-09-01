"""T011 (US1) — REST ``GET /inbox/{agent}/{path}`` returns reference.

For an over-threshold inbox bundle the REST read route returns the
``mode: "reference"`` envelope. It does NOT write a claim sidecar
(REST get is non-destructive for the inbox, symmetric with store —
sidecar only on pick). The per-file URLs are fetchable.
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend

from .conftest import seed_inbox_bundle


@pytest.mark.asyncio
async def test_rest_get_inbox_returns_reference_no_sidecar(
    core_app_small_inline: tuple[httpx.AsyncClient, str], h,
) -> None:
    client, _ = core_app_small_inline

    sender_key = await h.setup_agent(client, "bob", "main")
    alice_key = await h.create_key(client, sender_key, "alice", "main")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    files = [
        ("a.bin", b"A" * 200),
        ("b.bin", b"B" * 200),
    ]
    await seed_inbox_bundle(
        backend, "alice:main", "archive",
        files=files, sender="bob",
    )

    headers = {"Authorization": f"Bearer {alice_key}"}
    resp = await client.get("/inbox/alice:main/archive", headers=headers)
    assert resp.status_code == 200, (resp.status_code, resp.text[:400])
    assert resp.headers["content-type"].startswith("application/json")
    envelope = resp.json()
    assert envelope["mode"] == "reference", envelope
    assert "download_token" in envelope
    assert len(envelope["files"]) == 2
    for entry in envelope["files"]:
        assert "url" in entry
        assert "content" not in entry

    # REST get does NOT write a claim sidecar.
    claims_dir = storage_root / ".claims"
    if claims_dir.exists():
        assert list(claims_dir.glob("*.json")) == []

    # URLs are fetchable.
    token = envelope["download_token"]
    dl_headers = {"Authorization": f"Bearer {token}", "X-Sender": "alice:main"}
    seen: dict[str, bytes] = {}
    for entry in envelope["files"]:
        url_path = urllib.parse.urlparse(entry["url"]).path
        dl = await client.get(url_path, headers=dl_headers)
        assert dl.status_code == 200, (dl.status_code, dl.text[:200])
        seen[entry["name"]] = dl.content
    for name, data in files:
        assert seen[name] == data
