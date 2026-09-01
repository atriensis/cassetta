"""T010 (US1) — ``cassetta_get`` on a store bundle returns reference.

A 2 MB file in the store is retrieved via ``cassetta_get``; the envelope
is ``mode: "reference"`` and NO claim sidecar is written (FR-010a). The
per-file URL returns the bytes; the store entry remains readable after
(non-destructive per FR-009).
"""

from __future__ import annotations

import json
import os
import urllib.parse
from pathlib import Path

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend

from .conftest import seed_store_bundle


def _unwrap(result: dict) -> dict | str:
    text = result["content"][0]["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@pytest.mark.asyncio
async def test_get_store_returns_reference_without_sidecar(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app_small_inline

    api_key = await h.setup_agent(client, "bob", "store-ref")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    payload = b"X" * (2 * 1024 * 1024)  # 2 MB — way past the 32-byte inline cap
    await seed_store_bundle(
        backend,
        "docs/big-blob",
        files=[("big-blob.bin", payload)],
    )

    sid = await h.mcp_init(client, api_key=api_key)
    result = await h.mcp_call(
        client,
        "cassetta_get",
        {"path": "docs/big-blob"},
        sid=sid,
        api_key=api_key,
    )
    envelope = _unwrap(result)
    assert isinstance(envelope, dict), envelope
    assert envelope["mode"] == "reference", envelope
    assert "download_token" in envelope
    assert len(envelope["files"]) == 1
    assert "content" not in envelope["files"][0]

    # FR-010a: store reference gets NO claim sidecar.
    claims_dir = storage_root / ".claims"
    if claims_dir.exists():
        sidecars = list(claims_dir.glob("*.json"))
        assert sidecars == [], f"store reference MUST NOT write a claim sidecar; found: {sidecars!r}"

    token = envelope["download_token"]
    url_path = urllib.parse.urlparse(envelope["files"][0]["url"]).path
    headers = {"Authorization": f"Bearer {token}", "X-Sender": "bob:store-ref"}
    resp = await client.get(url_path, headers=headers)
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    assert resp.content == payload

    # Store entry remains readable (non-destructive — FR-009).
    again = await h.mcp_call(
        client,
        "cassetta_get",
        {"path": "docs/big-blob"},
        sid=sid,
        api_key=api_key,
    )
    again_env = _unwrap(again)
    assert isinstance(again_env, dict)
    assert again_env["mode"] == "reference", again_env
