"""Tests for MCP endpoint authentication middleware."""

import logging
import os
import tempfile
from dataclasses import dataclass, replace
from typing import ClassVar

import httpx
import pytest

from cassetta.app import create_app
from cassetta.config import load_config
from cassetta.defaults.factory import build_core_defaults
from cassetta.mcp_server import configure as configure_mcp


def _setup_app(setup_token: str, storage_dir: str) -> tuple:
    """Create app with MCP. Returns (app, key_store)."""
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    configure_mcp(config, backends)
    return app, backends.key_store


@dataclass
class _MetricCall:
    method: str
    name: str
    value: float | int
    tags: dict[str, str] | None


class _RecordingMetrics:
    kind: ClassVar[str] = "test"

    def __init__(self) -> None:
        self.calls: list[_MetricCall] = []

    def increment(
        self,
        name: str,
        value: int = 1,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(_MetricCall("increment", name, value, tags))

    def observe(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(_MetricCall("observe", name, value, tags))

    def gauge(
        self,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(_MetricCall("gauge", name, value, tags))


def _setup_app_with_metrics(
    setup_token: str,
    storage_dir: str,
) -> tuple:
    """Variant of ``_setup_app`` with a recording metrics provider."""
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"

    metrics = _RecordingMetrics()
    config = load_config()
    backends = replace(
        build_core_defaults(config),
        metrics_provider=metrics,
    )
    app = create_app(config, backends=backends)
    configure_mcp(config, backends)
    return app, backends.key_store, metrics


@pytest.mark.asyncio
async def test_mcp_rejects_unauthenticated_request() -> None:
    """MCP endpoint returns 401 when no auth header is provided."""
    storage_dir = tempfile.mkdtemp()
    app, _ks = _setup_app("secret-token", storage_dir)

    async with app.state.mcp_server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:16001",
        ) as client:
            response = await client.post("/mcp/", json={})
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_mcp_rejects_invalid_api_key() -> None:
    """MCP endpoint returns 401 when an invalid API key is provided."""
    storage_dir = tempfile.mkdtemp()
    app, _ks = _setup_app("secret-token", storage_dir)

    async with app.state.mcp_server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:16001",
        ) as client:
            response = await client.post(
                "/mcp/",
                json={},
                headers={"Authorization": "Bearer cst_invalid_key_here"},
            )
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_mcp_allows_valid_api_key() -> None:
    """MCP endpoint allows requests with a valid API key."""
    storage_dir = tempfile.mkdtemp()
    app, key_store = _setup_app("secret-token", storage_dir)
    raw_key, _info = await key_store.setup("test-agent")

    async with app.state.mcp_server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:16001",
        ) as client:
            response = await client.post(
                "/mcp/",
                json={},
                headers={"Authorization": f"Bearer {raw_key}"},
            )
            assert response.status_code != 401


@pytest.mark.asyncio
async def test_mcp_dev_mode_bypasses_auth() -> None:
    """MCP endpoint allows unauthenticated requests in dev mode."""
    storage_dir = tempfile.mkdtemp()
    app, _ks = _setup_app("", storage_dir)

    async with app.state.mcp_server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:16001",
        ) as client:
            response = await client.post("/mcp/", json={})
            assert response.status_code != 401


# `auth.failure` emission on the MCP path:
# missing Bearer prefix → reason=missing_bearer.
# invalid bearer → reason=invalid_key, identity_hint=first 12 chars.


def _failure_records(
    captured: list[logging.LogRecord],
) -> list[logging.LogRecord]:
    return [r for r in captured if getattr(r, "event", None) == "auth.failure"]


@pytest.mark.asyncio
async def test_mcp_missing_bearer_emits_auth_failure(
    auth_log_capture: list[logging.LogRecord],
) -> None:
    """US1 AS-4: MCP request without Bearer prefix emits source=mcp,
    reason=missing_bearer."""
    storage_dir = tempfile.mkdtemp()
    app, _ks, metrics = _setup_app_with_metrics("secret-tok-529", storage_dir)

    async with app.state.mcp_server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:16001",
        ) as client:
            response = await client.post("/mcp/", json={})

    assert response.status_code == 401
    records = _failure_records(auth_log_capture)
    assert len(records) == 1
    assert records[0].detail == {  # type: ignore[attr-defined]
        "source": "mcp",
        "reason": "missing_bearer",
        "identity_hint": None,
    }
    increments = [c for c in metrics.calls if c.name == "cassetta.auth.failures" and c.method == "increment"]
    assert len(increments) == 1
    assert increments[0].tags == {
        "source": "mcp",
        "reason": "missing_bearer",
    }
    # Note: R5 originally assumed MCP requests would have request_id=null.
    # In practice, RequestIdMiddleware is registered on the parent FastAPI
    # app and intercepts /mcp/ requests, so request_id IS attached. Carry
    # this finding into the notes file (deviation from research.md R5).
    assert getattr(records[0], "request_id", None) is not None


@pytest.mark.asyncio
async def test_mcp_invalid_bearer_emits_auth_failure_invalid_key(
    auth_log_capture: list[logging.LogRecord],
) -> None:
    """US1 AS-5: MCP request with invalid bearer emits source=mcp,
    reason=invalid_key, identity_hint=first 12 chars."""
    storage_dir = tempfile.mkdtemp()
    app, _ks, metrics = _setup_app_with_metrics("secret-tok-529", storage_dir)
    bearer = "fake-mcp-token-1234567890"

    async with app.state.mcp_server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:16001",
        ) as client:
            response = await client.post(
                "/mcp/",
                json={},
                headers={"Authorization": f"Bearer {bearer}"},
            )

    assert response.status_code == 401
    records = _failure_records(auth_log_capture)
    assert len(records) == 1
    detail = records[0].detail  # type: ignore[attr-defined]
    assert detail["source"] == "mcp"
    assert detail["reason"] == "invalid_key"
    assert detail["identity_hint"] == bearer[:12]
    assert len(detail["identity_hint"]) <= 12

    increments = [c for c in metrics.calls if c.name == "cassetta.auth.failures" and c.method == "increment"]
    assert len(increments) == 1
    assert increments[0].tags == {
        "source": "mcp",
        "reason": "invalid_key",
    }
