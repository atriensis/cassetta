"""Brief 533 FR-007 / FR-007a / SC-016 — MCP ``_enforce`` denial counter."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import ClassVar

import httpx
from _obs_helpers import CassettaLogCapture, RecordingMetricsProvider

from cassetta.protocols.identity import Identity

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


async def test_mcp_enforce_denial_increments_policy_decisions(
    storage_dir,
    recording_metrics: RecordingMetricsProvider,
    cassetta_log_capture: CassettaLogCapture,
) -> None:
    """An MCP tool call denied by ``_enforce`` increments
    ``cassetta.policy.decisions{result=denied, policy_kind=core}`` AND emits
    ``policy.denied`` with ``policy_kind=core`` in the event detail.
    """
    import asyncio

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
    cassetta_log_capture.attach()

    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
        ) as client:
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            }
            init_resp = await client.post(
                "/mcp/",
                json=init,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
            sid = init_resp.headers.get("mcp-session-id", "")
            recording_metrics.calls.clear()
            cassetta_log_capture.clear()
            call = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "cassetta_list",
                    "arguments": {},
                },
            }
            resp = await client.post(
                "/mcp/",
                json=call,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "mcp-session-id": sid,
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            # MCP error reports back to the caller (isError or content text).
            assert body["result"].get("isError") is True or (
                "Forbidden" in body["result"]["content"][0].get("text", "")
            )
    finally:
        stop.set()
        await task

    policy = recording_metrics.find("cassetta.policy.decisions", "increment")
    assert any(c.tags and c.tags.get("result") == "denied" and c.tags.get("policy_kind") == "core" for c in policy), [
        c.tags for c in policy
    ]

    denied_events = [r for r in cassetta_log_capture.records if getattr(r, "event", None) == "policy.denied"]
    assert denied_events
    detail = getattr(denied_events[0], "detail", {}) or {}
    assert detail.get("policy_kind") == "core"
