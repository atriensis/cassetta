"""Tests for agent discovery — GET /agents and cassetta_agents() MCP tool.

Visibility filter on the agents listing.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp
from cassetta.protocols.identity import Identity


class _StubVisibilityPolicy:
    """Stub policy to control visible_agents behavior."""

    kind = "stub"

    def __init__(self, *, visible_fn=None) -> None:
        self._visible_fn = visible_fn or (lambda identity, labels: list(labels))

    async def check(self, identity: Identity, resource: str, action: str) -> bool:
        return True

    async def visible_agents(self, identity: Identity, labels: list[str]) -> list[str]:
        result = self._visible_fn(identity, labels)
        if asyncio.iscoroutine(result):
            return await result
        return result


class _RecordingMetrics:
    kind = "recording"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def increment(self, name: str, value: int = 1, tags=None) -> None:
        self.events.append({"name": name, "value": value, "tags": tags or {}})

    def observe(self, name, value, tags=None) -> None:
        pass

    def gauge(self, name, value, tags=None) -> None:
        pass


@pytest.fixture
def storage_dir() -> str:
    return tempfile.mkdtemp()


async def _start_mcp(app):
    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()

    async def _stop() -> None:
        stop.set()
        await task

    return _stop


@pytest.fixture
async def stub_agents_app(
    storage_dir: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any, str]]:
    setup_token = "stub-agents-token"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_PER_FILE_MAX"] = "1048576"
    os.environ.pop("CASSETTA_MAX_FILE_SIZE", None)
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    app = create_app()
    config = app.state.config
    backends = app.state.backends

    sender_raw, _info = await backends.key_store.setup("test:sender")
    for label in ["test:bob", "test:carol", "test:dan"]:
        await backends.key_store.create_key(label)

    backends = replace(backends, metrics_provider=_RecordingMetrics())
    app.state.backends = backends
    configure_mcp(config, backends)

    stop_mcp = await _start_mcp(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as c:
        yield c, app, sender_raw

    await stop_mcp()


def _swap_policy(app, policy) -> None:
    app.state.backends = replace(app.state.backends, access_policy=policy)
    configure_mcp(app.state.config, app.state.backends)


# ---------------------------------------------------------------------------
# The agents endpoint itself, with no visibility policy in play.
# ---------------------------------------------------------------------------


class TestAgentsEndpoint:
    @pytest.mark.asyncio
    async def test_list_agents_returns_all_labels(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, _app, sender_key = stub_agents_app

        resp = await client.get(
            "/agents",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        labels = [a["label"] for a in data["agents"]]
        # DefaultAccessPolicy passthrough — all 4 labels visible.
        assert "test:sender" in labels
        assert "test:bob" in labels
        assert "test:carol" in labels
        assert "test:dan" in labels
        assert len(labels) == 4

    @pytest.mark.asyncio
    async def test_agents_includes_created_at(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, _app, sender_key = stub_agents_app
        resp = await client.get(
            "/agents",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        data = resp.json()
        for a in data["agents"]:
            assert "created_at" in a

    @pytest.mark.asyncio
    async def test_agents_excludes_revoked_keys(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_agents_app
        await app.state.backends.key_store.revoke_key("test:dan")

        resp = await client.get(
            "/agents",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        labels = [a["label"] for a in resp.json()["agents"]]
        assert "test:dan" not in labels

    @pytest.mark.asyncio
    async def test_agents_requires_auth(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, _app, _sender_key = stub_agents_app
        resp = await client.get("/agents")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Visibility filter.
# ---------------------------------------------------------------------------


class TestAgentsDefaultPolicyPassthrough:
    """DefaultAccessPolicy returns the full list unchanged."""

    @pytest.mark.asyncio
    async def test_default_policy_returns_all_labels(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, _app, sender_key = stub_agents_app
        resp = await client.get(
            "/agents",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        labels = {a["label"] for a in resp.json()["agents"]}
        assert {"test:sender", "test:bob", "test:carol", "test:dan"} <= labels


class TestAgentsListMetric:
    """The cassetta.agents.list metric tag."""

    @pytest.mark.asyncio
    async def test_unfiltered_when_full_list_returned(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_agents_app
        await client.get(
            "/agents",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        events = [e for e in app.state.backends.metrics_provider.events if e["name"] == "cassetta.agents.list"]
        assert events
        assert any(e["tags"].get("result") == "unfiltered" for e in events)

    @pytest.mark.asyncio
    async def test_filtered_when_subset_returned(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_agents_app
        _swap_policy(
            app,
            _StubVisibilityPolicy(
                visible_fn=lambda i, labels: ["test:bob"],
            ),
        )
        await client.get(
            "/agents",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        events = [e for e in app.state.backends.metrics_provider.events if e["name"] == "cassetta.agents.list"]
        assert events
        assert any(e["tags"].get("result") == "filtered" for e in events)


class TestAgentsRESTVisibilityFilter:
    """The visibility filter is applied to the REST listing."""

    @pytest.mark.asyncio
    async def test_visibility_filter_subsets_response(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_agents_app
        _swap_policy(
            app,
            _StubVisibilityPolicy(
                visible_fn=lambda i, labels: ["test:bob"],
            ),
        )
        resp = await client.get(
            "/agents",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        labels = {a["label"] for a in resp.json()["agents"]}
        assert labels == {"test:bob"}

    @pytest.mark.asyncio
    async def test_visibility_failure_returns_empty(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_agents_app

        def boom(identity, labels):
            raise RuntimeError("policy down")

        _swap_policy(app, _StubVisibilityPolicy(visible_fn=boom))
        resp = await client.get(
            "/agents",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        assert resp.json()["agents"] == []
        # Failure metric is tagged 'filtered' (zero-visibility outcome).
        events = [e for e in app.state.backends.metrics_provider.events if e["name"] == "cassetta.agents.list"]
        assert any(e["tags"].get("result") == "filtered" for e in events)


class TestAgentsMCPVisibilityFilter:
    """MCP cassetta_agents matches REST behavior."""

    @pytest.mark.asyncio
    async def test_mcp_passthrough_returns_all(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        from cassetta import mcp_server

        _client, _app, _sender_key = stub_agents_app
        mcp_server.set_current_identity(Identity(label="test:sender"))
        try:
            text = await mcp_server._cassetta_agents()
        finally:
            mcp_server.set_current_identity(None)
        agents = json.loads(text)
        labels = {a["label"] for a in agents}
        assert {"test:sender", "test:bob", "test:carol", "test:dan"} <= labels

    @pytest.mark.asyncio
    async def test_mcp_filtered_subset(
        self,
        stub_agents_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        from cassetta import mcp_server

        _client, app, _sender_key = stub_agents_app
        _swap_policy(
            app,
            _StubVisibilityPolicy(
                visible_fn=lambda i, labels: ["test:bob"],
            ),
        )
        mcp_server.set_current_identity(Identity(label="test:sender"))
        try:
            text = await mcp_server._cassetta_agents()
        finally:
            mcp_server.set_current_identity(None)
        agents = json.loads(text)
        labels = {a["label"] for a in agents}
        assert labels == {"test:bob"}

        # Metric also fires from MCP path.
        events = [e for e in app.state.backends.metrics_provider.events if e["name"] == "cassetta.agents.list"]
        assert any(e["tags"].get("result") == "filtered" for e in events)
