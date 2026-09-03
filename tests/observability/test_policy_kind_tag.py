"""The additive ``policy_kind`` tag/field."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import ClassVar

import httpx
from _obs_helpers import CassettaLogCapture, RecordingMetricsProvider

from cassetta.protocols.identity import Identity

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


async def test_core_denial_has_policy_kind_core_field(
    storage_dir,
    recording_metrics: RecordingMetricsProvider,
    cassetta_log_capture: CassettaLogCapture,
) -> None:
    """A core REST `_enforce` denial emits `policy.denied`
    with `policy_kind=core` field AND `cassetta.policy.decisions{policy_kind=core}`
    counter tag.
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
    # Attach AFTER configure_logging clears the cassetta logger's handlers.
    cassetta_log_capture.attach()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        recording_metrics.calls.clear()
        cassetta_log_capture.clear()
        resp = await client.get("/inbox/x/")
        assert resp.status_code == 403

    # Counter has policy_kind=core tag.
    policy = recording_metrics.find("cassetta.policy.decisions", "increment")
    assert any(c.tags and c.tags.get("policy_kind") == "core" for c in policy), [c.tags for c in policy]

    # Event has policy_kind=core in its detail field.
    denied = [r for r in cassetta_log_capture.records if getattr(r, "event", None) == "policy.denied"]
    assert denied, "expected at least one policy.denied event"
    detail = getattr(denied[0], "detail", {}) or {}
    assert detail.get("policy_kind") == "core", detail


async def test_additive_tag_preserves_sum(
    storage_dir,
    recording_metrics: RecordingMetricsProvider,
) -> None:
    """Aggregations summing cassetta.policy.decisions{result=denied} while
    ignoring policy_kind report the SAME total before and after the tag was
    added, in a core-only deployment.
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
        for _ in range(3):
            await client.get("/inbox/x/")

    # Total denials = 3 regardless of grouping.
    denials = [
        c
        for c in recording_metrics.find("cassetta.policy.decisions", "increment")
        if c.tags and c.tags.get("result") == "denied"
    ]
    assert len(denials) == 3
    # Pre-brief filter (ignoring policy_kind) still sees them all.
    by_result = sum(1 for c in denials if c.tags and c.tags.get("result") == "denied")
    assert by_result == 3
