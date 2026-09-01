"""Tests for broadcast — POST /broadcast and cassetta_broadcast() MCP tool.

Brief 525 — visibility-first pipeline:
    list_keys → visible_agents → resolver → policy.check → write
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp
from cassetta.protocols.alias import ResolvedRecipient
from cassetta.protocols.identity import Identity


class _RecordingMetricsProvider:
    """Test metrics provider that records all increments for assertion."""

    kind = "recording"

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def increment(
        self,
        name: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.events.append({"name": name, "value": value, "tags": tags or {}})

    def observe(self, name: str, value: float, tags=None) -> None:
        pass

    def gauge(self, name: str, value: float, tags=None) -> None:
        pass


class _StubAccessPolicy:
    """Stub policy with controllable visible_agents + check behavior."""

    kind = "stub"

    def __init__(
        self,
        *,
        visible_fn=None,
        check_fn=None,
    ) -> None:
        self._visible_fn = visible_fn or (lambda identity, labels: list(labels))
        self._check_fn = check_fn or (lambda identity, resource, action: True)

    async def check(
        self, identity: Identity, resource: str, action: str
    ) -> bool:
        result = self._check_fn(identity, resource, action)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def visible_agents(
        self, identity: Identity, labels: list[str]
    ) -> list[str]:
        result = self._visible_fn(identity, labels)
        if asyncio.iscoroutine(result):
            return await result
        return result


class _StubAliasResolver:
    """Stub resolver — by default, every label maps to itself."""

    kind = "stub"

    def __init__(self, *, resolve_fn=None) -> None:
        self._resolve_fn = resolve_fn

    async def resolve(
        self, name: str, *, sender_label: str | None = None
    ) -> ResolvedRecipient | None:
        if self._resolve_fn is None:
            return ResolvedRecipient(inbox_targets=[name])
        result = self._resolve_fn(name, sender_label)
        if asyncio.iscoroutine(result):
            return await result
        return result


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
async def stub_app(storage_dir: str) -> AsyncIterator[tuple[httpx.AsyncClient, Any, str]]:
    """Auth-enabled app with sender pre-provisioned + helper hooks for stubbing.

    Yields (client, app, sender_api_key).
    """
    setup_token = "stub-broadcast-token"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_PER_FILE_MAX"] = "1048576"
    os.environ.pop("CASSETTA_MAX_FILE_SIZE", None)
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = (
        "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    )
    os.environ["CASSETTA_JWT_KEY"] = (
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    )
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    key_store = backends.key_store

    sender_raw, _info = await key_store.setup("test:sender")
    # Provision a couple of additional active keys to broadcast to.
    for label in ["test:bob", "test:carol", "test:dan"]:
        await key_store.create_key(label)

    metrics = _RecordingMetricsProvider()
    backends = replace(backends, metrics_provider=metrics)
    app.state.backends = backends
    configure_mcp(config, backends)

    stop_mcp = await _start_mcp(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        yield client, app, sender_raw

    await stop_mcp()


def _swap_policy(app, *, policy=None, resolver=None) -> None:
    overrides: dict[str, Any] = {}
    if policy is not None:
        overrides["access_policy"] = policy
    if resolver is not None:
        overrides["alias_resolver"] = resolver
    app.state.backends = replace(app.state.backends, **overrides)
    configure_mcp(app.state.config, app.state.backends)


# ---------------------------------------------------------------------------
# Pre-Brief-525 baseline tests (still useful)
# ---------------------------------------------------------------------------


class TestBroadcastEndpoint:
    @pytest.mark.asyncio
    async def test_broadcast_delivers_to_all_visible(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app

        resp = await client.post(
            "/broadcast/hello.txt",
            content=b"broadcast message",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # DefaultAccessPolicy passthrough — all 3 active labels visible.
        assert data["total_delivered"] == 3
        assert data["total_failed"] == 0
        assert data["total_denied"] == 0
        assert "test:sender" not in data["delivered_to"]

    @pytest.mark.asyncio
    async def test_broadcast_zero_recipients(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app
        # Stub visibility to empty.
        _swap_policy(app, policy=_StubAccessPolicy(
            visible_fn=lambda identity, labels: [],
        ))
        resp = await client.post(
            "/broadcast/lonely.txt",
            content=b"lonely message",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_delivered"] == 0
        assert data["delivered_to"] == []
        assert data["denied"] == []
        assert data["failed"] == []

    @pytest.mark.asyncio
    async def test_broadcast_requires_path(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, _app, sender_key = stub_app
        # No path segment → no route matches; FastAPI returns 307/404/405.
        resp = await client.post(
            "/broadcast",
            content=b"no path",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code in (307, 404, 405)


# ---------------------------------------------------------------------------
# Brief 525 — Phase 3 tests
# ---------------------------------------------------------------------------


class TestBroadcastVisibilityFirst:
    """T010 — SC-001 anti-leak invariant."""

    @pytest.mark.asyncio
    async def test_invisible_targets_not_in_response(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app

        # visible_agents returns only "test:bob" — carol & dan are invisible.
        _swap_policy(app, policy=_StubAccessPolicy(
            visible_fn=lambda identity, labels: ["test:bob"],
        ))

        resp = await client.post(
            "/broadcast/hello.txt",
            content=b"x",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["delivered_to"] == ["test:bob"]
        assert data["denied"] == []
        assert data["failed"] == []
        # SC-001: invisible labels MUST NOT appear anywhere in the response.
        body_text = resp.text
        assert "test:carol" not in body_text
        assert "test:dan" not in body_text


class TestBroadcastDeniedBucket:
    """T011 — denied bucket + denial metric."""

    @pytest.mark.asyncio
    async def test_denied_target_in_denied_bucket_with_metric(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app

        def check_fn(identity, resource, action):
            return resource != "inbox:test:bob"

        _swap_policy(app, policy=_StubAccessPolicy(check_fn=check_fn))

        resp = await client.post(
            "/broadcast/hello.txt",
            content=b"x",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        denied_targets = [d["target"] for d in data["denied"]]
        assert "test:bob" in denied_targets
        for d in data["denied"]:
            if d["target"] == "test:bob":
                assert d["reason"] == "access_denied"
        assert "test:bob" not in data["delivered_to"]

        metrics = app.state.backends.metrics_provider
        denials = [
            (m["name"], m["tags"]) for m in metrics.events
            if m["name"] == "cassetta.policy.decisions"
        ]
        # Exactly one denial event for the one denied target.
        write_denials = [
            tags for name, tags in denials
            if tags.get("result") == "denied" and tags.get("action") == "write"
        ]
        assert len(write_denials) == 1


class TestBroadcastFailedBucket:
    """T012 — failed bucket (resolver_unknown + write_error)."""

    @pytest.mark.asyncio
    async def test_resolver_returns_none_target_in_failed(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app

        def resolve_fn(name, sender_label):
            if name == "test:bob":
                return None
            return ResolvedRecipient(inbox_targets=[name])

        _swap_policy(app, resolver=_StubAliasResolver(resolve_fn=resolve_fn))

        resp = await client.post(
            "/broadcast/hello.txt",
            content=b"x",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        failed_targets = {f["target"]: f for f in data["failed"]}
        assert "test:bob" in failed_targets
        assert failed_targets["test:bob"]["error"] == "resolver_unknown"
        assert "test:bob" not in data["delivered_to"]

    @pytest.mark.asyncio
    async def test_backend_write_exception_target_in_failed(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app

        original_open = app.state.backends.backend.open_bundle_write

        async def raising_open(bundle_path: str):
            if "test:bob" in bundle_path:
                raise RuntimeError("disk on fire")
            return await original_open(bundle_path)

        with patch.object(
            app.state.backends.backend, "open_bundle_write", raising_open,
        ):
            resp = await client.post(
                "/broadcast/hello.txt",
                content=b"x",
                headers={"Authorization": f"Bearer {sender_key}"},
            )

        assert resp.status_code == 200
        data = resp.json()
        failed_targets = {f["target"]: f for f in data["failed"]}
        assert "test:bob" in failed_targets
        assert failed_targets["test:bob"]["error"].startswith("write_error: ")


class TestBroadcastOperationsResultTag:
    """T013 — cassetta.broadcast.operations result tag matrix."""

    @pytest.mark.asyncio
    async def test_all_delivered_success(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app
        resp = await client.post(
            "/broadcast/ok.txt",
            content=b"x",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        metrics = app.state.backends.metrics_provider
        ops = [
            m for m in metrics.events
            if m["name"] == "cassetta.broadcast.operations"
        ]
        assert ops
        assert any(m["tags"].get("result") == "success" for m in ops)

    @pytest.mark.asyncio
    async def test_mixed_partial(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app
        _swap_policy(app, policy=_StubAccessPolicy(
            check_fn=lambda i, r, a: r != "inbox:test:bob",
        ))
        resp = await client.post(
            "/broadcast/mixed.txt",
            content=b"x",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        metrics = app.state.backends.metrics_provider
        partial = [
            m for m in metrics.events
            if m["name"] == "cassetta.broadcast.operations"
            and m["tags"].get("result") == "partial"
        ]
        assert partial

    @pytest.mark.asyncio
    async def test_zero_visible_noop(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app
        _swap_policy(app, policy=_StubAccessPolicy(
            visible_fn=lambda i, labels: [],
        ))
        resp = await client.post(
            "/broadcast/empty.txt",
            content=b"x",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        metrics = app.state.backends.metrics_provider
        noop = [
            m for m in metrics.events
            if m["name"] == "cassetta.broadcast.operations"
            and m["tags"].get("result") == "noop"
        ]
        assert noop

    @pytest.mark.asyncio
    async def test_visible_but_zero_delivered_error(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app
        # All visible, all denied.
        _swap_policy(app, policy=_StubAccessPolicy(
            check_fn=lambda i, r, a: False,
        ))
        resp = await client.post(
            "/broadcast/alldenied.txt",
            content=b"x",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        metrics = app.state.backends.metrics_provider
        err = [
            m for m in metrics.events
            if m["name"] == "cassetta.broadcast.operations"
            and m["tags"].get("result") == "error"
        ]
        assert err


class TestBroadcastWholePolicyFailure:
    """T014 — fail-closed when visible_agents raises."""

    @pytest.mark.asyncio
    async def test_visible_agents_exception_returns_empty_response(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
        caplog,
    ) -> None:
        import logging as _logging
        client, app, sender_key = stub_app

        def boom(identity, labels):
            raise RuntimeError("policy is unwell")

        _swap_policy(app, policy=_StubAccessPolicy(visible_fn=boom))

        cassetta_logger = _logging.getLogger("cassetta")
        previous_propagate = cassetta_logger.propagate
        cassetta_logger.propagate = True
        try:
            with caplog.at_level("ERROR", logger="cassetta"):
                resp = await client.post(
                    "/broadcast/boom.txt",
                    content=b"x",
                    headers={"Authorization": f"Bearer {sender_key}"},
                )
        finally:
            cassetta_logger.propagate = previous_propagate

        assert resp.status_code == 200
        data = resp.json()
        assert data["delivered_to"] == []
        assert data["denied"] == []
        assert data["failed"] == []

        msgs = [r.getMessage() for r in caplog.records]
        assert any("broadcast.visibility_failed" in m for m in msgs), msgs

        metrics = app.state.backends.metrics_provider
        err = [
            m for m in metrics.events
            if m["name"] == "cassetta.broadcast.operations"
            and m["tags"].get("result") == "error"
        ]
        assert err


class TestBroadcastPerTargetCheckException:
    """T015 — policy.check raising for one target → that target denied,
    others proceed."""

    @pytest.mark.asyncio
    async def test_per_target_check_exception(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app

        def check_fn(identity, resource, action):
            if resource == "inbox:test:bob":
                raise RuntimeError("policy crash for bob")
            return True

        _swap_policy(app, policy=_StubAccessPolicy(check_fn=check_fn))

        resp = await client.post(
            "/broadcast/err.txt",
            content=b"x",
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        denied_targets = {d["target"]: d["reason"] for d in data["denied"]}
        assert denied_targets.get("test:bob") == "check_error"
        # Carol and Dan delivered normally.
        assert "test:carol" in data["delivered_to"]
        assert "test:dan" in data["delivered_to"]


class TestBroadcastOversize:
    """T009 — oversize payload returns 413, no bundles written."""

    @pytest.mark.asyncio
    async def test_oversize_returns_413(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        client, app, sender_key = stub_app

        # CASSETTA_MAX_FILE_SIZE=1048576 (1 MiB). Send 2 MiB.
        big = b"x" * (2 * 1024 * 1024)
        resp = await client.post(
            "/broadcast/big.txt",
            content=big,
            headers={"Authorization": f"Bearer {sender_key}"},
        )
        assert resp.status_code == 413
        body = resp.json()
        assert body["error"] == "cap_exceeded"

        # No bundles written — backend root has no inbox subdirs.
        backend = app.state.backends.backend
        # Check that no inbox/<target>/big.txt bundles exist.
        for label in ["test:bob", "test:carol", "test:dan"]:
            try:
                await backend.open_bundle_file_read(
                    f"inbox/{label}/big.txt", "big.txt",
                )
                pytest.fail(f"Bundle should not exist for {label}")
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# MCP tests (T016 + T017)
# ---------------------------------------------------------------------------


class TestMCPBroadcastVisibility:
    """T016 — MCP visibility-first filtering."""

    @pytest.mark.asyncio
    async def test_invisible_labels_not_in_textual_return(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        from cassetta import mcp_server

        _client, app, _sender_key = stub_app
        _swap_policy(app, policy=_StubAccessPolicy(
            visible_fn=lambda identity, labels: ["test:bob"],
        ))

        # Set up the contextvars the MCP tool reads.
        mcp_server.set_sender_label("test:sender")
        mcp_server.set_current_identity(
            Identity(label="test:sender", extra={"user_id": "u-1"})
        )
        try:
            text = await mcp_server._cassetta_broadcast(
                path="hello.txt", content="hi"
            )
        finally:
            mcp_server.set_sender_label(None)
            mcp_server.set_current_identity(None)

        assert "test:bob" in text
        # SC-001 invariant — invisible names are absent.
        assert "test:carol" not in text
        assert "test:dan" not in text


class TestMCPBroadcastDeniedBucket:
    """T017 — MCP denied bucket + denial metric."""

    @pytest.mark.asyncio
    async def test_denied_targets_dont_appear_and_metric_fires(
        self, stub_app: tuple[httpx.AsyncClient, Any, str],
    ) -> None:
        from cassetta import mcp_server

        _client, app, _sender_key = stub_app

        def check_fn(identity, resource, action):
            return resource != "inbox:test:bob"

        _swap_policy(app, policy=_StubAccessPolicy(check_fn=check_fn))

        mcp_server.set_sender_label("test:sender")
        mcp_server.set_current_identity(
            Identity(label="test:sender", extra={"user_id": "u-1"})
        )
        try:
            text = await mcp_server._cassetta_broadcast(
                path="hello.txt", content="hi"
            )
        finally:
            mcp_server.set_sender_label(None)
            mcp_server.set_current_identity(None)

        # Textual return surfaces the denied count without enumerating labels.
        assert "1 denied" in text or "denied" in text
        # Bob (denied) does not appear in the recipient name list.
        assert "test:bob" not in text

        metrics = app.state.backends.metrics_provider
        write_denials = [
            m for m in metrics.events
            if m["name"] == "cassetta.policy.decisions"
            and m["tags"].get("result") == "denied"
            and m["tags"].get("action") == "write"
        ]
        assert len(write_denials) == 1
