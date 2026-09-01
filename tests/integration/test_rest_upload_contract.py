"""T027 — REST contract test for ``POST /upload/{bundle_path:path}`` (Brief 514).

Asserts the endpoint exists in the FastAPI app's OpenAPI schema with the
expected route + security + response codes.
"""

from __future__ import annotations

import io
import json
import tarfile
import urllib.parse

import httpx
import pytest


def _tar_one(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_upload_route_registered(
    core_app: tuple[httpx.AsyncClient, str],
) -> None:
    client, _ = core_app
    app = client._transport.app  # type: ignore[attr-defined]
    schema = app.openapi()
    paths = schema.get("paths", {})
    assert any(p.startswith("/upload/") for p in paths), (
        f"no /upload/{{bundle_path}} in OpenAPI paths, got keys {sorted(paths)}"
    )


@pytest.mark.asyncio
async def test_upload_route_requires_bearer(
    core_app: tuple[httpx.AsyncClient, str],
) -> None:
    client, _ = core_app
    # Bare request, no Authorization header → 401 missing_bearer.
    resp = await client.post("/upload/inbox%2Falice%2Fnotes.md", content=b"")
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"]["error"] == "unauthenticated"
    assert body["detail"]["reason"] == "missing_bearer"


@pytest.mark.asyncio
async def test_upload_route_rejects_inline_token(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """A token whose mode=='inline' must not be accepted at /upload/..."""
    import json

    client, _ = core_app_small_inline
    sender = await h.setup_agent(client, "bob", "inlinetoken")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)
    init = await h.mcp_call(
        client,
        "cassetta_send_init",
        {
            "to": "alice:main",
            "path": "t.md",
            "manifest": {"file_count": 1, "files": [{"name": "a.txt", "size": 3}]},
        },
        sid=sid,
        api_key=sender,
    )
    body = json.loads(init["content"][0]["text"])
    assert body["mode"] == "inline"
    token = body["inline_token"]

    # Force batch path post with this inline token.
    resp = await client.post(
        "/upload/inbox%2Falice%3Amain%2Ft.md",
        content=b"",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-tar"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["error"] == "unauthenticated"
    assert body["detail"]["reason"] == "wrong_mode"


@pytest.mark.asyncio
async def test_rest_size_mismatch_keeps_wrong_size_reason(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """Brief 541 boundary pin: the REST tar upload path carries no per-file
    ``encoding``, so a genuine size mismatch must still report
    ``reason=wrong_size``. The inline ``missing_or_bad_encoding`` relabel is
    MCP-inline-only and must not leak onto this path.
    """
    client, _ = core_app_small_inline
    sender = await h.setup_agent(client, "bob", "rest-wrongsize")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    # Manifest declares 100 bytes; the tar member carries 200 → size mismatch.
    init = await h.mcp_call(
        client,
        "cassetta_send_init",
        {
            "to": "alice:main",
            "path": "rest.md",
            "manifest": {"file_count": 1, "files": [{"name": "payload.bin", "size": 100}]},
        },
        sid=sid,
        api_key=sender,
    )
    body = json.loads(init["content"][0]["text"])
    token = body["batch_token"]
    url_path = urllib.parse.urlparse(body["upload_url"]).path

    resp = await client.post(
        url_path,
        content=_tar_one("payload.bin", b"x" * 200),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-tar"},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "manifest_violation"
    assert detail["reason"] == "wrong_size"
    assert detail["reason"] != "missing_or_bad_encoding"
