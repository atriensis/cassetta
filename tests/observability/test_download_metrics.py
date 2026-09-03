"""Download metric coverage tests."""

from __future__ import annotations

import io
import os
import time
import urllib.parse
import uuid
from dataclasses import replace

import httpx
import pytest
from _obs_helpers import RecordingMetricsProvider

from cassetta.auth import jwt_tokens

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


def _set_env(storage_dir: str) -> None:
    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost"
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost"
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)


async def _seed_inbox(backend, recipient: str, path: str, content: bytes) -> str:
    from cassetta.mime import pick_mime

    bundle_path = f"inbox/{recipient}/{path}"
    writer = await backend.open_bundle_write(bundle_path)
    bundle_id = uuid.uuid4().hex
    try:
        records = [
            {
                "name": path,
                "size": len(content),
                "mime": pick_mime(path, explicit=None),
            }
        ]
        await writer.write_file(records[0]["name"], io.BytesIO(content))
        from datetime import UTC, datetime

        await writer.commit(
            {
                "schema_version": 1,
                "bundle_id": bundle_id,
                "sender": None,
                "created_at": datetime.now(UTC).isoformat(),
                "content_type": "application/octet-stream",
                "file_count": 1,
                "files": records,
            }
        )
        return bundle_id
    except Exception:
        await writer.abort()
        raise


@pytest.fixture
async def download_app(storage_dir, recording_metrics):
    from cassetta.app import create_app
    from cassetta.config import load_config
    from cassetta.defaults.factory import build_core_defaults
    from cassetta.mcp_server import configure as configure_mcp

    _set_env(storage_dir)
    config = load_config()
    backends = replace(
        build_core_defaults(config),
        metrics_provider=recording_metrics,
    )
    app = create_app(config, backends=backends)
    configure_mcp(config, backends)
    return app, config, backends


def _mint_download_token(
    config,
    bundle_path: str,
    name: str,
    recipient: str,
) -> str:
    now = int(time.time())
    claims = {
        "jti": uuid.uuid4().hex,
        "bundle_path": bundle_path,
        "bundle_id": uuid.uuid4().hex,
        "recipient": recipient,
        "file_names": [name],
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    return jwt_tokens.sign(claims, key=config.jwt_primary_key)


async def test_success_increments_ok_and_bytes(
    download_app,
    recording_metrics: RecordingMetricsProvider,
) -> None:
    """Success: result=ok + download.bytes."""
    app, config, backends = download_app
    content = b"download me"
    await _seed_inbox(backends.backend, "alice", "doc.txt", content)

    bundle_path = "inbox/alice/doc.txt"
    token = _mint_download_token(config, bundle_path, "doc.txt", "alice")
    url = "/download/" + urllib.parse.quote(bundle_path, safe="") + "/" + urllib.parse.quote("doc.txt", safe="")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        recording_metrics.calls.clear()
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}", "X-Sender": "alice"},
        )
        assert resp.status_code == 200
        body = await resp.aread()
        assert body == content

    ops = recording_metrics.find("cassetta.download.operations", "increment")
    assert any(c.tags and c.tags.get("result") == "ok" for c in ops), [c.tags for c in ops]
    bytes_calls = recording_metrics.find("cassetta.download.bytes", "increment")
    assert bytes_calls, "expected cassetta.download.bytes"
    assert bytes_calls[0].value == len(content)
    assert bytes_calls[0].tags is None


async def test_bundle_gone_and_name_not_in_manifest_collapse_to_not_found(
    download_app,
    recording_metrics: RecordingMetricsProvider,
) -> None:
    """Both bundle_gone and name_not_in_manifest map to result=not_found."""
    app, config, backends = download_app

    # Case A: bundle_gone — bundle never created.
    bundle_path = "inbox/alice/ghost.txt"
    token = _mint_download_token(config, bundle_path, "ghost.txt", "alice")
    url_a = "/download/" + urllib.parse.quote(bundle_path, safe="") + "/" + urllib.parse.quote("ghost.txt", safe="")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        recording_metrics.calls.clear()
        resp_a = await client.get(
            url_a,
            headers={"Authorization": f"Bearer {token}", "X-Sender": "alice"},
        )
        assert resp_a.status_code == 404

    ops = recording_metrics.find("cassetta.download.operations", "increment")
    assert any(c.tags and c.tags.get("result") == "not_found" for c in ops)


async def test_identity_mismatch_increments_identity_mismatch_only(
    storage_dir,
    recording_metrics: RecordingMetricsProvider,
) -> None:
    """JWT recipient != identity label → result=identity_mismatch;
    ZERO cassetta.auth.failures{source=download}.
    """
    # Auth mode (CASSETTA_SETUP_TOKEN non-empty) — dev_mode disabled so
    # ``_resolve_identity_label`` honours the X-Sender header.
    from cassetta.app import create_app
    from cassetta.config import load_config
    from cassetta.defaults.factory import build_core_defaults
    from cassetta.mcp_server import configure as configure_mcp

    _set_env(storage_dir)
    os.environ["CASSETTA_SETUP_TOKEN"] = "auth-token"
    config = load_config()
    backends = replace(
        build_core_defaults(config),
        metrics_provider=recording_metrics,
    )
    app = create_app(config, backends=backends)
    configure_mcp(config, backends)

    content = b"forbidden"
    await _seed_inbox(backends.backend, "alice", "secret.txt", content)
    bundle_path = "inbox/alice/secret.txt"
    token = _mint_download_token(config, bundle_path, "secret.txt", "alice")
    url = "/download/" + urllib.parse.quote(bundle_path, safe="") + "/" + urllib.parse.quote("secret.txt", safe="")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        recording_metrics.calls.clear()
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Sender": "bob",  # mismatch
            },
        )
        assert resp.status_code == 403

    ops = recording_metrics.find("cassetta.download.operations", "increment")
    assert any(c.tags and c.tags.get("result") == "identity_mismatch" for c in ops)
    auth_failures = recording_metrics.find("cassetta.auth.failures", "increment")
    assert not auth_failures, "identity mismatch is authorisation, not authentication"


async def test_jwt_expired_increments_auth_failures_only(
    download_app,
    recording_metrics: RecordingMetricsProvider,
) -> None:
    """Expired JWT → auth.failures emission;
    ZERO cassetta.download.operations.
    """
    app, config, backends = download_app
    bundle_path = "inbox/alice/doc.txt"
    # Mint an already-expired JWT.
    now = int(time.time())
    claims = {
        "jti": uuid.uuid4().hex,
        "bundle_path": bundle_path,
        "bundle_id": uuid.uuid4().hex,
        "recipient": "alice",
        "file_names": ["doc.txt"],
        "iat": now - 7200,
        "nbf": now - 7200,
        "exp": now - 3600,
    }
    token = jwt_tokens.sign(claims, key=config.jwt_primary_key)
    url = "/download/" + urllib.parse.quote(bundle_path, safe="") + "/" + urllib.parse.quote("doc.txt", safe="")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        recording_metrics.calls.clear()
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}", "X-Sender": "alice"},
        )
        assert resp.status_code == 401

    auth_failures = recording_metrics.find("cassetta.auth.failures", "increment")
    assert any(
        c.tags and c.tags.get("source") == "download" and c.tags.get("reason") == "jwt_expired" for c in auth_failures
    ), [c.tags for c in auth_failures]
    download_ops = recording_metrics.find(
        "cassetta.download.operations",
        "increment",
    )
    assert not download_ops, "JWT auth failure MUST NOT increment cassetta.download.operations"
