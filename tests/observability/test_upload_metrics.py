"""Upload metric coverage tests."""

from __future__ import annotations

import io
import os
import tarfile
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


@pytest.fixture
async def upload_client(
    storage_dir: str,
    recording_metrics: RecordingMetricsProvider,
):
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        recording_metrics.calls.clear()
        yield client, recording_metrics, config


def _mint_batch_token(
    config,
    bundle_path: str,
    manifest_files: list[dict],
) -> str:
    now = int(time.time())
    claims = {
        "bundle_path": bundle_path,
        "bundle_id": uuid.uuid4().hex,
        "sender": "test",
        "recipient": bundle_path.split("/")[1] if bundle_path.startswith("inbox/") else "",
        "mode": "batch",
        "manifest": {"file_count": len(manifest_files), "files": manifest_files},
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    return jwt_tokens.sign(claims, key=config.jwt_primary_key)


def _make_tar(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def test_success_increments_ok_and_bytes(upload_client) -> None:
    """Success path: result=ok + upload.bytes with bytes committed."""
    client, metrics, config = upload_client
    content = b"hello world"
    manifest = [{"name": "a.txt", "size": len(content), "mime": "text/plain"}]
    bundle_path = "inbox/alice/note1.tar"
    token = _mint_batch_token(config, bundle_path, manifest)
    body = _make_tar([("a.txt", content)])

    resp = await client.post(
        "/upload/" + urllib.parse.quote(bundle_path, safe=""),
        content=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text

    ops = metrics.find("cassetta.upload.operations", "increment")
    assert any(c.tags and c.tags.get("result") == "ok" for c in ops), [c.tags for c in ops]
    bytes_calls = metrics.find("cassetta.upload.bytes", "increment")
    assert bytes_calls, "expected cassetta.upload.bytes increment"
    assert bytes_calls[0].value == len(content)
    assert bytes_calls[0].tags is None


async def test_manifest_rejection_increments_rejected_and_violation(
    upload_client,
) -> None:
    """Manifest violation: result=rejected + manifest_violations{reason}
    AND ZERO upload.bytes.
    """
    client, metrics, config = upload_client
    # Declared 5 bytes, but archive contains 10 → wrong_size violation.
    manifest = [{"name": "a.txt", "size": 5, "mime": "text/plain"}]
    bundle_path = "inbox/alice/wrongsize.tar"
    token = _mint_batch_token(config, bundle_path, manifest)
    body = _make_tar([("a.txt", b"1234567890")])  # 10 bytes != 5

    resp = await client.post(
        "/upload/" + urllib.parse.quote(bundle_path, safe=""),
        content=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400

    ops = metrics.find("cassetta.upload.operations", "increment")
    assert any(c.tags and c.tags.get("result") == "rejected" for c in ops)
    violations = metrics.find("cassetta.upload.manifest_violations", "increment")
    assert violations, "expected cassetta.upload.manifest_violations"
    assert violations[0].tags is not None and "reason" in violations[0].tags

    bytes_calls = metrics.find("cassetta.upload.bytes", "increment")
    assert not bytes_calls, "rejection MUST NOT increment upload.bytes"


async def test_midstream_error_increments_error_only(upload_client) -> None:
    """Mid-stream tar parse error: result=error AND ZERO upload.bytes."""
    client, metrics, config = upload_client
    manifest = [{"name": "a.txt", "size": 5, "mime": "text/plain"}]
    bundle_path = "inbox/alice/garbage.tar"
    token = _mint_batch_token(config, bundle_path, manifest)
    # Garbage body — tarfile will raise TarError mid-stream.
    body = b"this is not a tar archive"

    resp = await client.post(
        "/upload/" + urllib.parse.quote(bundle_path, safe=""),
        content=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (400, 422, 500), resp.text

    ops = metrics.find("cassetta.upload.operations", "increment")
    # tar parse error counts as a mid-stream error.
    assert any(c.tags and c.tags.get("result") == "error" for c in ops), [c.tags for c in ops]
    bytes_calls = metrics.find("cassetta.upload.bytes", "increment")
    assert not bytes_calls, "mid-stream error MUST NOT increment upload.bytes"


async def test_denied_403_does_not_increment_upload_counters(
    storage_dir,
    recording_metrics: RecordingMetricsProvider,
) -> None:
    """A 401 (no bearer) MUST NOT increment cassetta.upload.* — the metric
    is emitted only after JWT verification (which is the auth boundary)."""
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        recording_metrics.calls.clear()
        # No Authorization → 401.
        resp = await client.post(
            "/upload/inbox%2Falice%2Fx.tar",
            content=b"",
        )
        assert resp.status_code == 401

    upload_metrics = [c for c in recording_metrics.calls if c.name.startswith("cassetta.upload.")]
    assert not upload_metrics
