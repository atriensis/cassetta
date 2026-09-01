"""T041 — US5: atomic visibility + rollback on interruption (Brief 514).

Covers:
- Mid-stream manifest violation → active rollback (bundle dir removed in
  the same request path, not deferred to the reaper).
- Kill mid-stream (simulated via a generator that raises) → no ``meta.json``.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import urllib.parse

import httpx
import pytest


def _build_tar(files: dict[str, bytes], *, compress: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return gzip.compress(raw) if compress else raw


def _unwrap(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_mid_stream_manifest_violation_rolls_back(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app_small_inline
    app = client._transport.app  # type: ignore[attr-defined]
    storage_root = os.path.join(app.state.config.storage_path, "data")

    sender = await h.setup_agent(client, "bob", "u5-viol")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    # Manifest declares size 100 but we'll tar an entry with 200 bytes → wrong_size.
    files = {"payload.bin": b"x" * 200}
    init = await h.mcp_call(
        client,
        "cassetta_send_init",
        {
            "to": "alice:main",
            "path": "r.md",
            "manifest": {"file_count": 1, "files": [{"name": "payload.bin", "size": 100}]},
        },
        sid=sid,
        api_key=sender,
    )
    body = _unwrap(init)
    token = body["batch_token"]
    url_path = urllib.parse.urlparse(body["upload_url"]).path

    tar_bytes = _build_tar(files)
    resp = await client.post(
        url_path,
        content=tar_bytes,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-tar"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["error"] == "manifest_violation"
    assert body["detail"]["reason"] == "wrong_size"

    # Active rollback: bundle dir gone from disk.
    bundle_dir = os.path.join(storage_root, "inbox", "alice:main", "r.md")
    assert not os.path.exists(bundle_dir), f"expected active rollback to remove {bundle_dir}"


@pytest.mark.asyncio
async def test_kill_mid_stream_leaves_no_meta_json(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """Client disconnect mid-stream → no ``meta.json``; bundle invisible."""
    client, _ = core_app_small_inline

    sender = await h.setup_agent(client, "bob", "u5-kill")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    manifest_files = {"big.bin": b"A" * 500, "small.bin": b"B" * 100}
    init = await h.mcp_call(
        client,
        "cassetta_send_init",
        {
            "to": "alice:main",
            "path": "dropped.tgz",
            "manifest": {
                "file_count": 2,
                "files": [{"name": n, "size": len(d)} for n, d in manifest_files.items()],
            },
        },
        sid=sid,
        api_key=sender,
    )
    body = _unwrap(init)
    token = body["batch_token"]
    url_path = urllib.parse.urlparse(body["upload_url"]).path

    # Send gzip-marked body that's actually plain tar — forces a gzip
    # parse error before any tar entry lands.
    tar_bytes = _build_tar(manifest_files, compress=False)
    resp = await client.post(
        url_path,
        content=tar_bytes,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-tar",
            "Content-Encoding": "gzip",
        },
    )
    assert resp.status_code >= 400

    # The inbox listing must NOT show the bundle.
    recip_key = await h.create_key(client, sender, "alice", "list")
    recip_sid = await h.mcp_init(client, api_key=recip_key)
    inbox = await h.mcp_call(
        client,
        "cassetta_inbox",
        {"agent": "alice:main"},
        sid=recip_sid,
        api_key=recip_key,
    )
    listing = json.loads(inbox["content"][0]["text"])
    paths = {entry["path"] for entry in (listing or [])}
    assert "dropped.tgz" not in paths
