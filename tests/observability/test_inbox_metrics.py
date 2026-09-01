"""Inbox metric coverage tests — Brief 533 FR-001 + SC-013/SC-014/SC-015."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import ClassVar

import httpx
import pytest
from _obs_helpers import RecordingMetricsProvider

from cassetta.protocols.identity import Identity

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


async def _seed_inbox(backend, recipient: str, path: str, content: bytes) -> None:
    import io
    import uuid
    from datetime import UTC, datetime

    from cassetta.mime import pick_mime

    bundle_path = f"inbox/{recipient}/{path}"
    writer = await backend.open_bundle_write(bundle_path)
    try:
        records = [
            {
                "name": path.split("/")[-1],
                "size": len(content),
                "mime": pick_mime(path, explicit=None),
            }
        ]
        await writer.write_file(records[0]["name"], io.BytesIO(content))
        await writer.commit(
            {
                "schema_version": 1,
                "bundle_id": uuid.uuid4().hex,
                "sender": None,
                "created_at": datetime.now(UTC).isoformat(),
                "content_type": "application/octet-stream",
                "file_count": 1,
                "files": records,
            }
        )
    except Exception:
        await writer.abort()
        raise


@pytest.fixture
async def inbox_client(
    storage_dir: str,
    recording_metrics: RecordingMetricsProvider,
):
    """Dev-mode client with a seeded inbox bundle at ``inbox/alice/hi.txt``."""
    from cassetta.app import create_app
    from cassetta.config import load_config
    from cassetta.defaults.factory import build_core_defaults
    from cassetta.mcp_server import configure as configure_mcp

    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost"
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost"
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)

    config = load_config()
    backends = replace(
        build_core_defaults(config),
        metrics_provider=recording_metrics,
    )
    app = create_app(config, backends=backends)
    configure_mcp(config, backends)
    await _seed_inbox(backends.backend, "alice", "hi.txt", b"hello")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        recording_metrics.calls.clear()
        yield client, recording_metrics


async def test_list_increments_action_list(inbox_client) -> None:
    client, metrics = inbox_client
    resp = await client.get("/inbox/alice/")
    assert resp.status_code == 200
    ops = metrics.find("cassetta.inbox.operations", "increment")
    assert [c.tags for c in ops if c.tags and c.tags.get("action") == "list"]


async def test_read_increments_action_read(inbox_client) -> None:
    client, metrics = inbox_client
    resp = await client.get("/inbox/alice/hi.txt")
    assert resp.status_code == 200
    ops = metrics.find("cassetta.inbox.operations", "increment")
    assert [c.tags for c in ops if c.tags and c.tags.get("action") == "read"]


async def test_pick_increments_action_pick(inbox_client) -> None:
    client, metrics = inbox_client
    resp = await client.post("/inbox/alice/hi.txt/pick")
    assert resp.status_code == 200
    ops = metrics.find("cassetta.inbox.operations", "increment")
    assert [c.tags for c in ops if c.tags and c.tags.get("action") == "pick"]


async def test_delete_increments_action_delete(inbox_client) -> None:
    client, metrics = inbox_client
    resp = await client.delete("/inbox/alice/hi.txt")
    assert resp.status_code == 204
    ops = metrics.find("cassetta.inbox.operations", "increment")
    assert [c.tags for c in ops if c.tags and c.tags.get("action") == "delete"]


async def test_404_not_found_still_increments(inbox_client) -> None:
    """SC-014 — authorised inbox read returning 404 STILL increments read."""
    client, metrics = inbox_client
    resp = await client.get("/inbox/alice/no-such-file")
    assert resp.status_code == 404
    ops = metrics.find("cassetta.inbox.operations", "increment")
    assert [c.tags for c in ops if c.tags and c.tags.get("action") == "read"]


async def test_peek_emission_unchanged(inbox_client) -> None:
    """SC-015 — existing peek emission preserved after FR-063 backfill."""
    client, metrics = inbox_client
    resp = await client.get("/inbox/alice/hi.txt/peek")
    assert resp.status_code == 200
    ops = metrics.find("cassetta.inbox.operations", "increment")
    peeks = [c for c in ops if c.tags and c.tags.get("action") == "peek"]
    assert len(peeks) == 1


async def test_denied_403_does_not_increment(
    storage_dir,
    recording_metrics: RecordingMetricsProvider,
    policy_log_capture,
) -> None:
    """SC-013 — denied inbox read does NOT increment inbox.operations;
    DOES increment cassetta.policy.decisions{result=denied, policy_kind=core}.
    """
    from cassetta.app import create_app
    from cassetta.config import load_config
    from cassetta.defaults.factory import build_core_defaults
    from cassetta.mcp_server import configure as configure_mcp

    class DenyAllPolicy:
        kind: ClassVar[str] = "deny-all"

        async def check(
            self,
            identity: Identity,
            resource: str,
            action: str,
        ) -> bool:
            return False

        async def visible_agents(
            self,
            identity: Identity,
            labels: list[str],
        ) -> list[str]:
            return []

    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost"
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost"
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)

    config = load_config()
    backends = replace(
        build_core_defaults(config),
        metrics_provider=recording_metrics,
        access_policy=DenyAllPolicy(),
    )
    app = create_app(config, backends=backends)
    configure_mcp(config, backends)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        recording_metrics.calls.clear()
        resp = await client.get("/inbox/alice/hi.txt")
        assert resp.status_code == 403

    inbox_ops = recording_metrics.find("cassetta.inbox.operations", "increment")
    assert not inbox_ops, (
        f"Denied request must NOT increment cassetta.inbox.operations, got {[c.tags for c in inbox_ops]}"
    )
    policy = recording_metrics.find("cassetta.policy.decisions", "increment")
    assert len(policy) == 1, [c.tags for c in policy]
    assert policy[0].tags == {"result": "denied", "policy_kind": "core"}
