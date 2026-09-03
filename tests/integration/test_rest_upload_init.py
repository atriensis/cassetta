"""Integration tests for the REST upload-init route ``POST /uploads``.

Covers the US1 acceptance scenarios: a pure-HTTP (no-MCP) client completes a full
directed send; the route requires a ``cst_`` key (401 without); a recipient-
visibility denial mirrors the MCP send-init (403); ``/capabilities`` advertises the
REST send-init; and the generated OpenAPI request schema is typed (carries the
manifest + destination fields, not an additional-properties-only object).
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import tempfile
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp
from cassetta.protocols.identity import Identity


def _build_tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# --- US1 scenario 1: full directed send over plain HTTP ---------------------


@pytest.mark.asyncio
async def test_rest_uploads_happy_path_end_to_end(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "rest-init")
    recipient_key = await h.create_key(client, sender_key, "alice", "main")

    content = b"hello cassetta over plain http!"
    files = {"hello.txt": content}

    # Phase 1 — create the upload session over REST with the sender's cst_ key.
    init = await client.post(
        "/uploads",
        json={
            "to": "alice:main",
            "path": "drop.tgz",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "hello.txt", "size": len(content)}],
            },
        },
        headers=h.auth(sender_key),
    )
    assert init.status_code == 201, (init.status_code, init.text)
    body = init.json()
    assert body.keys() == {"mode", "bundle_id", "upload_url", "batch_token", "expires_at"}
    assert body["mode"] == "batch"
    assert body["upload_url"].startswith("http://localhost:16001/upload/")
    assert "%2F" in body["upload_url"], "bundle_path must be URL-encoded"
    bundle_id = body["bundle_id"]

    # Phase 2 — stream the tar to upload_url with the batch_token (unchanged route).
    url_path = urllib.parse.urlparse(body["upload_url"]).path
    resp = await client.post(
        url_path,
        content=_build_tar(files),
        headers={
            "Authorization": f"Bearer {body['batch_token']}",
            "Content-Type": "application/x-tar",
        },
    )
    assert resp.status_code == 201, (resp.status_code, resp.text)
    assert resp.json() == {"bundle_id": bundle_id, "ok": True}

    # The bundle is delivered to the recipient's inbox (zero MCP used to send).
    recip_sid = await h.mcp_init(client, api_key=recipient_key)
    inbox = await h.mcp_call(
        client,
        "cassetta_inbox",
        {"agent": "alice:main"},
        sid=recip_sid,
        api_key=recipient_key,
    )
    listing = json.loads(inbox["content"][0]["text"])
    assert "drop.tgz" in {entry["path"] for entry in listing}


# --- US1 scenario 2: authentication required --------------------------------


@pytest.mark.asyncio
async def test_rest_uploads_requires_auth(
    core_app: tuple[httpx.AsyncClient, str],
) -> None:
    client, _ = core_app
    resp = await client.post(
        "/uploads",
        json={
            "to": "alice:main",
            "path": "drop.tgz",
            "manifest": {"file_count": 1, "files": [{"name": "a.txt", "size": 3}]},
        },
    )
    assert resp.status_code == 401, (resp.status_code, resp.text)


# --- US1 scenario 3: recipient-visibility denial mirrors send-init ----------


class _AlwaysDenyPolicy:
    async def check(self, identity: Identity, resource: str, action: str) -> bool:
        return False


@pytest.fixture
async def deny_app(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Auth-enabled app whose access policy denies every check.

    A key is bootstrapped *before* the deny policy is swapped in (otherwise key
    creation itself would be denied). Yields ``(client, raw_key)``.
    """
    storage_dir = tempfile.mkdtemp()
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "deny-test-token")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", storage_dir)
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.setenv("CASSETTA_MAX_FILE_SIZE", "1048576")
    monkeypatch.setenv(
        "CASSETTA_MCP_ALLOWED_HOSTS",
        "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001",
    )
    monkeypatch.setenv(
        "CASSETTA_JWT_KEY",
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0",
    )
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    monkeypatch.delenv("CASSETTA_KEYS_FILE", raising=False)

    app = create_app()
    config = app.state.config
    default_backends = app.state.backends
    raw_key, _ = await default_backends.key_store.setup("bob:deny")

    app.state.backends = replace(default_backends, access_policy=_AlwaysDenyPolicy())
    configure_mcp(config, app.state.backends)

    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        yield client, raw_key
    stop.set()
    await task


@pytest.mark.asyncio
async def test_rest_uploads_denied_mirrors_send_init(
    deny_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, raw_key = deny_app
    # A bare recipient name passes alias-resolution through to the access check.
    payload: dict[str, Any] = {
        "to": "carol",
        "path": "x.txt",
        "manifest": {"file_count": 1, "files": [{"name": "x.txt", "size": 3}]},
    }

    # REST: the access denial surfaces as 403 Forbidden (same shape as other routes).
    rest = await client.post("/uploads", json=payload, headers=h.auth(raw_key))
    assert rest.status_code == 403, (rest.status_code, rest.text)
    assert rest.json() == {"detail": "Forbidden"}

    # MCP: the same denial on cassetta_send_init — the REST route grants no
    # capability the MCP tool lacks.
    sid = await h.mcp_init(client, api_key=raw_key)
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "cassetta_send_init", "arguments": payload},
    }
    resp = await h.mcp_post(client, body, sid=sid, api_key=raw_key)
    assert resp.status_code == 200
    text = json.dumps(resp.json())
    assert "Forbidden" in text


# --- US3 (covered here per Done-when): advertise + self-documenting schema ---


@pytest.mark.asyncio
async def test_capabilities_advertises_rest_send_init(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/capabilities")
    assert resp.status_code == 200
    assert "rest_send_init" in resp.json()["features"]


@pytest.mark.asyncio
async def test_openapi_uploads_request_is_typed(
    client: httpx.AsyncClient,
) -> None:
    schema = (await client.get("/openapi.json")).json()
    post_op = schema["paths"]["/uploads"]["post"]
    req_schema = post_op["requestBody"]["content"]["application/json"]["schema"]
    assert req_schema.get("$ref", "").endswith("/UploadSessionRequest")

    schemas = schema["components"]["schemas"]
    req_model = schemas["UploadSessionRequest"]
    props = req_model["properties"]
    assert {"to", "path", "manifest"} <= set(props)
    # The manifest field is the typed SendManifest model (not a free-form object).
    assert props["manifest"].get("$ref", "").endswith("/SendManifest")
    assert req_model.get("additionalProperties", False) is not True
    assert "SendManifest" in schemas
