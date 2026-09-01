"""T013 (US1) — FR-006a two-factor contract on GET /download.

Exercises the three-way failure matrix from Q3:
(a) valid JWT + mismatched identity (X-Sender) → 403 identity_mismatch.
(b) valid JWT + no identity header → 401 (identity-resolution rejects).
(c) no Authorization header + valid identity → 401 missing_bearer.

Every negative case MUST produce a body with zero file bytes.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend

from .conftest import seed_inbox_bundle


def _unwrap(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


async def _make_envelope(
    client: httpx.AsyncClient, h, alice_key: str,
) -> dict:
    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    await seed_inbox_bundle(
        backend, "alice:main", "secret-drop",
        files=[("payload.bin", b"x" * 256)],
        sender="bob",
    )
    sid = await h.mcp_init(client, api_key=alice_key)
    result = await h.mcp_call(
        client, "cassetta_pick", {"path": "secret-drop"},
        sid=sid, api_key=alice_key,
    )
    envelope = _unwrap(result)
    assert envelope["mode"] == "reference", envelope
    return envelope


def _download_path(envelope: dict) -> str:
    url = envelope["files"][0]["url"]
    return urllib.parse.urlparse(url).path


@pytest.mark.asyncio
async def test_identity_mismatch_403(
    core_app_small_inline: tuple[httpx.AsyncClient, str], h,
) -> None:
    """Valid JWT (recipient=alice:main) + X-Sender: mallory → 403."""
    client, _ = core_app_small_inline
    sender = await h.setup_agent(client, "bob", "main")
    alice = await h.create_key(client, sender, "alice", "main")
    envelope = await _make_envelope(client, h, alice)

    resp = await client.get(
        _download_path(envelope), headers={
            "Authorization": f"Bearer {envelope['download_token']}",
            "X-Sender": "mallory",
        },
    )
    assert resp.status_code == 403, (resp.status_code, resp.text[:200])
    body = resp.json()
    assert body["error"] == "forbidden"
    assert body["reason"] == "identity_mismatch"
    # Zero file bytes in the body: response is JSON error, not the file.
    assert resp.content != b"x" * 256


@pytest.mark.asyncio
async def test_identity_missing_401(
    core_app_small_inline: tuple[httpx.AsyncClient, str], h,
) -> None:
    """Valid JWT + no identity header (X-Sender absent) → 401."""
    client, _ = core_app_small_inline
    sender = await h.setup_agent(client, "bob", "main")
    alice = await h.create_key(client, sender, "alice", "main")
    envelope = await _make_envelope(client, h, alice)

    resp = await client.get(
        _download_path(envelope), headers={
            "Authorization": f"Bearer {envelope['download_token']}",
            # X-Sender omitted
        },
    )
    assert resp.status_code == 401, (resp.status_code, resp.text[:200])
    # Zero file bytes in the body.
    assert resp.content != b"x" * 256


@pytest.mark.asyncio
async def test_no_bearer_401_missing_bearer(
    core_app_small_inline: tuple[httpx.AsyncClient, str], h,
) -> None:
    """No Authorization header, identity set → 401 missing_bearer."""
    client, _ = core_app_small_inline
    sender = await h.setup_agent(client, "bob", "main")
    alice = await h.create_key(client, sender, "alice", "main")
    envelope = await _make_envelope(client, h, alice)

    resp = await client.get(
        _download_path(envelope), headers={
            "X-Sender": "alice:main",
        },
    )
    assert resp.status_code == 401, (resp.status_code, resp.text[:200])
    body = resp.json()
    assert body["error"] == "unauthenticated"
    assert body["reason"] == "missing_bearer"
    assert resp.content != b"x" * 256
