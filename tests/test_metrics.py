"""Tests for MetricsProvider protocol, wiring, and hook points."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cassetta.app import create_app
from cassetta.defaults.default_metrics import DefaultMetricsProvider
from cassetta.protocols.metrics import MetricsProvider


@dataclass
class MetricCall:
    method: str
    name: str
    value: float | int
    tags: dict[str, str] | None


class RecordingMetricsProvider:
    """T027: Test helper that records all metric calls for assertion."""

    kind = "test"

    def __init__(self) -> None:
        self.calls: list[MetricCall] = []

    def increment(
        self,
        name: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(MetricCall("increment", name, value, tags))

    def observe(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(MetricCall("observe", name, value, tags))

    def gauge(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(MetricCall("gauge", name, value, tags))

    def find(self, name: str, method: str | None = None) -> list[MetricCall]:
        return [c for c in self.calls if c.name == name and (method is None or c.method == method)]

    def has(self, name: str, method: str | None = None) -> bool:
        return len(self.find(name, method)) > 0


class TestDefaultMetricsProvider:
    """T039: No-op provider has negligible overhead."""

    def test_no_op_methods_do_nothing(self):
        provider = DefaultMetricsProvider()
        # Should not raise
        provider.increment("test", 1, {"key": "value"})
        provider.observe("test", 1.0, {"key": "value"})
        provider.gauge("test", 1.0, {"key": "value"})

    def test_satisfies_protocol(self):
        provider = DefaultMetricsProvider()
        assert isinstance(provider, MetricsProvider)

    def test_recording_satisfies_protocol(self):
        provider = RecordingMetricsProvider()
        assert isinstance(provider, MetricsProvider)


@pytest.fixture
def recording_metrics():
    return RecordingMetricsProvider()


@pytest.fixture
async def metrics_client(storage_dir, recording_metrics, make_backends):
    import os

    from cassetta.config import load_config
    from cassetta.mcp_server import configure as configure_mcp

    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost"
    os.environ["CASSETTA_JWT_KEY"] = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    config = load_config()
    app = create_app(
        config,
        backends=make_backends(config, metrics_provider=recording_metrics),
    )
    backends = app.state.backends
    configure_mcp(config, backends)

    import asyncio

    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run():
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        # Setup: create first key in dev mode
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "test-key"},
            headers={"Authorization": "Bearer setup-token-unused"},
        )
        if resp.status_code == 201:
            api_key = resp.json()["api_key"]
            client.headers["Authorization"] = f"Bearer {api_key}"
        # Register a bob key so inbox tests can resolve the recipient
        await backends.key_store.create_key("test:bob")
        recording_metrics.calls.clear()
        yield client, recording_metrics

    stop.set()
    await task


class TestRequestMetrics:
    """T034: Request-level metrics (total, duration, errors)."""

    @pytest.mark.asyncio
    async def test_requests_total_incremented(self, metrics_client):
        client, metrics = metrics_client
        await client.get("/files/")
        assert metrics.has("cassetta.requests.total", "increment")

    @pytest.mark.asyncio
    async def test_request_duration_observed(self, metrics_client):
        client, metrics = metrics_client
        await client.get("/files/")
        assert metrics.has("cassetta.request.duration_seconds", "observe")

    @pytest.mark.asyncio
    async def test_request_errors_on_4xx(self, metrics_client):
        client, metrics = metrics_client
        await client.get("/files/nonexistent-path-that-does-not-exist")
        assert metrics.has("cassetta.request.errors", "increment")


class TestFileMetrics:
    """T035: File operation metrics."""

    @pytest.mark.asyncio
    async def test_file_put_metrics(self, metrics_client):
        client, metrics = metrics_client
        await client.put("/files/metric-test.txt", content=b"hello")
        ops = metrics.find("cassetta.files.operations")
        assert any(c.tags and c.tags.get("action") == "put" for c in ops)
        bytes_calls = metrics.find("cassetta.files.bytes")
        assert any(c.tags and c.tags.get("action") == "put" for c in bytes_calls)

    @pytest.mark.asyncio
    async def test_file_get_metrics(self, metrics_client):
        client, metrics = metrics_client
        await client.put("/files/metric-get.txt", content=b"data")
        metrics.calls.clear()
        await client.get("/files/metric-get.txt")
        ops = metrics.find("cassetta.files.operations")
        assert any(c.tags and c.tags.get("action") == "get" for c in ops)

    @pytest.mark.asyncio
    async def test_file_delete_metrics(self, metrics_client):
        client, metrics = metrics_client
        await client.put("/files/metric-del.txt", content=b"data")
        metrics.calls.clear()
        await client.delete("/files/metric-del.txt")
        ops = metrics.find("cassetta.files.operations")
        assert any(c.tags and c.tags.get("action") == "delete" for c in ops)

    @pytest.mark.asyncio
    async def test_file_list_metrics(self, metrics_client):
        client, metrics = metrics_client
        await client.get("/files/")
        ops = metrics.find("cassetta.files.operations")
        assert any(c.tags and c.tags.get("action") == "list" for c in ops)


class TestInboxMetrics:
    """T036: Inbox operation metrics."""

    @pytest.mark.asyncio
    async def test_inbox_send_metrics(self, metrics_client):
        """Brief 514: inbox send metric is emitted by ``cassetta_send_init``."""
        import json

        client, metrics = metrics_client
        # MCP init
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "metrics-test", "version": "1.0.0"},
            },
        }
        init_resp = await client.post(
            "/mcp/",
            json=init_body,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        sid = init_resp.headers.get("mcp-session-id", "")
        # Trigger a send via the new flow
        call_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "cassetta_send_init",
                "arguments": {
                    "to": "test:bob",
                    "path": "hello.txt",
                    "manifest": {
                        "file_count": 1,
                        "files": [{"name": "hello.txt", "size": 6}],
                    },
                },
            },
        }
        resp = await client.post(
            "/mcp/",
            json=call_body,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "mcp-session-id": sid,
            },
        )
        result = resp.json()["result"]
        assert result.get("isError") is not True, result
        payload = json.loads(result["content"][0]["text"])
        assert "bundle_id" in payload
        ops = metrics.find("cassetta.inbox.operations")
        assert any(c.tags and c.tags.get("action") == "send" for c in ops)


class TestKeyMetrics:
    """T037: Key operation metrics."""

    @pytest.mark.asyncio
    async def test_key_create_metrics(self, metrics_client):
        client, metrics = metrics_client
        await client.post("/keys", json={"host": "test", "project": "metric-key"})
        ops = metrics.find("cassetta.keys.operations")
        assert any(c.tags and c.tags.get("action") == "create" for c in ops)
        assert metrics.has("cassetta.active_keys", "gauge")


class TestPolicyMetrics:
    """T038: Policy decision metrics."""

    @pytest.mark.asyncio
    async def test_policy_deny_emits_metric(self, storage_dir, make_backends):
        """Test that policy denials emit metrics via a deny-all policy."""
        import os
        from typing import ClassVar

        from cassetta.config import load_config
        from cassetta.mcp_server import configure as configure_mcp
        from cassetta.protocols.identity import Identity

        class DenyAllPolicy:
            kind: ClassVar[str] = "deny-all"

            async def check(self, identity: Identity, resource: str, action: str) -> bool:
                return False

        recording = RecordingMetricsProvider()
        os.environ["CASSETTA_SETUP_TOKEN"] = ""
        os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
        os.environ["CASSETTA_DEFAULT_TTL"] = "0"
        os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
        os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost"
        os.environ.pop("CASSETTA_KEYS_FILE", None)

        config = load_config()
        app = create_app(
            config,
            backends=make_backends(
                config,
                access_policy=DenyAllPolicy(),
                metrics_provider=recording,
            ),
        )
        backends = app.state.backends
        configure_mcp(config, backends)

        import asyncio

        started = asyncio.Event()
        stop_ev = asyncio.Event()

        async def _run():
            async with app.state.mcp_server.session_manager.run():
                started.set()
                await stop_ev.wait()

        task = asyncio.create_task(_run())
        await started.wait()

        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # Setup creates the first key (dev mode = empty token auth works)
            resp = await client.post(
                "/setup",
                json={"host": "test", "project": "test"},
                headers={"Authorization": "Bearer setup-token-unused"},
            )
            # The deny-all policy should cause a 403
            assert resp.status_code == 403
            decisions = recording.find("cassetta.policy.decisions")
            assert any(c.tags and c.tags.get("result") == "denied" for c in decisions)

        stop_ev.set()
        await task
