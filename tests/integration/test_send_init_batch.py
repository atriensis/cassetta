"""T029 — US2 integration: `send_init` (batch) + tar upload → REST.

Drives the full batch round-trip against an ASGI test client:
`cassetta_send_init` → batch JWT + URL → build a tar archive in memory
(matching the CLI's contract) → `POST /upload/{bundle_path}` →
receiver's inbox listing contains the bundle → `cassetta_pick`
returns byte-identical content.

We exercise the same HTTP path the CLI would drive, but via
``httpx.AsyncClient`` so the test stays in-process and fast. A separate
CLI test (see ``test_cli_upload_contract.py``) covers the argparse
contract without touching HTTP.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import urllib.parse

import httpx
import pytest


def _build_tar(files: dict[str, bytes], *, compress: bool) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return gzip.compress(raw) if compress else raw


def _unwrap(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
@pytest.mark.parametrize("compress", [True, False], ids=["gzip", "plain"])
async def test_batch_roundtrip(
    core_app_small_inline: tuple[httpx.AsyncClient, str], h, compress: bool,
) -> None:
    client, _ = core_app_small_inline
    sender_key = await h.setup_agent(client, "bob", f"batch-{compress}")
    await h.create_key(client, sender_key, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender_key)

    files = {
        "src/main.py": b"print('hello cassetta, batch edition')\n" * 2,
        "README.md": b"# hello\n" + b"x" * 200,
    }
    manifest = {
        "file_count": len(files),
        "files": [
            {"name": name, "size": len(data)} for name, data in files.items()
        ],
    }
    init = await h.mcp_call(
        client, "cassetta_send_init",
        {"to": "alice:main", "path": "project-drop.tgz", "manifest": manifest},
        sid=sid, api_key=sender_key,
    )
    body = _unwrap(init)
    assert body["mode"] == "batch", (
        f"expected batch for size-forcing test, got {body!r}"
    )
    upload_url = body["upload_url"]
    token = body["batch_token"]
    bundle_id = body["bundle_id"]

    tar_bytes = _build_tar(files, compress=compress)

    # Extract the URL-encoded path portion and POST to that relative URL
    # via the ASGI client.
    url_path = urllib.parse.urlparse(upload_url).path
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-tar",
    }
    if compress:
        headers["Content-Encoding"] = "gzip"
    resp = await client.post(url_path, content=tar_bytes, headers=headers)
    assert resp.status_code == 201, (resp.status_code, resp.text)
    assert resp.json() == {"bundle_id": bundle_id, "ok": True}

    # --- Receiver: list + pick ---------------------------------------------
    recipient_key = await h.create_key(client, sender_key, "alice", f"peek-{compress}")
    recip_sid = await h.mcp_init(client, api_key=recipient_key)
    # The recipient lives under alias `alice:main` — use that inbox.
    inbox = await h.mcp_call(
        client, "cassetta_inbox", {"agent": "alice:main"},
        sid=recip_sid, api_key=recipient_key,
    )
    listing = json.loads(inbox["content"][0]["text"])
    paths = {entry["path"] for entry in listing}
    assert "project-drop.tgz" in paths


@pytest.mark.asyncio
async def test_batch_wrong_bundle_path_rejected(
    core_app_small_inline: tuple[httpx.AsyncClient, str], h,
) -> None:
    """T030 — batch JWT issued for alice but POSTed to bob's path → 401."""
    client, _ = core_app_small_inline

    sender_key = await h.setup_agent(client, "bob", "mismatch")
    await h.create_key(client, sender_key, "alice", "main")
    await h.create_key(client, sender_key, "mallory", "main")
    sid = await h.mcp_init(client, api_key=sender_key)

    files = {"payload.bin": b"x" * 100}
    init = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "notes.md",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "payload.bin", "size": 100}],
            },
        },
        sid=sid, api_key=sender_key,
    )
    body = _unwrap(init)
    token = body["batch_token"]

    # Post to mallory's path instead.
    bad_url = "/upload/" + urllib.parse.quote("inbox/mallory:main/notes.md", safe="")
    tar_bytes = _build_tar(files, compress=False)
    resp = await client.post(
        bad_url,
        content=tar_bytes,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-tar"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "unauthenticated"
    assert resp.json()["detail"]["reason"] == "bundle_path_mismatch"
