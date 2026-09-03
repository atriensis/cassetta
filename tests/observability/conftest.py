"""Shared fixtures for ``tests/observability/``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

# Expose this directory on sys.path so tests can ``from _obs_helpers
# import ...`` without depending on relative-package imports (the
# project convention forbids ``__init__.py`` files in test dirs — see
# the pyproject.toml comment on the importlib mode).
_HERE = str(Path(__file__).parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from _obs_helpers import (  # noqa: E402 — sys.path injection above
    CassettaLogCapture,
    MetricCall,  # noqa: F401 — re-exported for tests that import the type
    RecordingMetricsProvider,
)


@pytest.fixture
def recording_metrics() -> RecordingMetricsProvider:
    return RecordingMetricsProvider()


_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


@pytest.fixture
async def obs_client(
    storage_dir: str,
    recording_metrics: RecordingMetricsProvider,
) -> AsyncIterator[tuple[httpx.AsyncClient, RecordingMetricsProvider]]:
    """Dev-mode client with a recording ``MetricsProvider``.

    Yields ``(client, recording_metrics)``. The MCP session manager is
    started so MCP tool calls work in the same client.
    """
    from cassetta.app import create_app
    from cassetta.config import load_config
    from cassetta.defaults.factory import build_core_defaults
    from cassetta.mcp_server import configure as configure_mcp

    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY_FILE", None)

    config = load_config()
    backends = replace(
        build_core_defaults(config),
        metrics_provider=recording_metrics,
    )
    app = create_app(config, backends=backends)
    configure_mcp(config, backends)

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
        # Seed a key for "test:bob" so inbox writes can resolve a recipient.
        await backends.key_store.create_key("test:bob")
        recording_metrics.calls.clear()
        yield client, recording_metrics

    stop.set()
    await task


@pytest.fixture
async def obs_auth_client(
    storage_dir: str,
    recording_metrics: RecordingMetricsProvider,
) -> AsyncIterator[tuple[httpx.AsyncClient, str, RecordingMetricsProvider]]:
    """Auth-enabled client with a recording ``MetricsProvider``.

    Yields ``(client, setup_token, recording_metrics)``.
    """
    from cassetta.app import create_app
    from cassetta.config import load_config
    from cassetta.defaults.factory import build_core_defaults
    from cassetta.mcp_server import configure as configure_mcp

    setup_token = "test-setup-token-533"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY_FILE", None)

    config = load_config()
    backends = replace(
        build_core_defaults(config),
        metrics_provider=recording_metrics,
    )
    app = create_app(config, backends=backends)
    configure_mcp(config, backends)

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
        yield client, setup_token, recording_metrics

    stop.set()
    await task


@pytest.fixture
def cassetta_log_capture() -> Iterator[CassettaLogCapture]:
    helper = CassettaLogCapture()
    try:
        yield helper
    finally:
        helper.detach()


@pytest.fixture
def policy_log_capture() -> Iterator[list[logging.LogRecord]]:
    """Backwards-compat: captures records emitted on ``cassetta`` after
    ``configure_logging``. Tests using this fixture MUST call
    ``create_app`` BEFORE referencing the captured list.
    """
    helper = CassettaLogCapture()
    # Attach lazily: the fixture user creates the app inside the test body;
    # we attach BEFORE returning so the handler survives create_app only if
    # the user wraps with .attach() themselves. The simpler interface here
    # exposes the records list and lets the user re-attach by accessing
    # the underlying helper through the .__attach__ wrapper below.
    helper.attach()
    try:
        yield helper.records
    finally:
        helper.detach()
