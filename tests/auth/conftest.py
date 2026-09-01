"""Shared fixtures for ``tests/auth/`` (Brief 529)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import ClassVar

import httpx
import pytest

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


@dataclass
class MetricCall:
    method: str
    name: str
    value: float | int
    tags: dict[str, str] | None


class RecordingMetricsProvider:
    """In-memory ``MetricsProvider`` that captures every call.

    Mirrors the helper used by ``tests/test_metrics.py``; duplicated
    here so the auth tests do not cross-import from a sibling test
    module. Implements the structural ``MetricsProvider`` Protocol from
    ``cassetta.protocols.metrics``.
    """

    kind: ClassVar[str] = "test"

    def __init__(self) -> None:
        self.calls: list[MetricCall] = []

    def increment(
        self, name: str, value: int = 1, tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(MetricCall("increment", name, value, tags))

    def observe(
        self, name: str, value: float, tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(MetricCall("observe", name, value, tags))

    def gauge(
        self, name: str, value: float, tags: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(MetricCall("gauge", name, value, tags))

    def find(
        self, name: str, method: str | None = None,
    ) -> list[MetricCall]:
        return [
            c for c in self.calls
            if c.name == name and (method is None or c.method == method)
        ]


@pytest.fixture
def recording_metrics() -> RecordingMetricsProvider:
    return RecordingMetricsProvider()


@pytest.fixture
async def auth_metrics_client(
    storage_dir: str, recording_metrics: RecordingMetricsProvider,
) -> AsyncIterator[tuple[httpx.AsyncClient, str, RecordingMetricsProvider]]:
    """Auth-enabled client with a recording ``MetricsProvider`` wired in.

    Yields ``(client, setup_token, recording_metrics)``.
    """
    from cassetta.app import create_app
    from cassetta.config import load_config
    from cassetta.defaults.factory import build_core_defaults
    from cassetta.mcp_server import configure as configure_mcp

    setup_token = "test-setup-token-529"
    os.environ["CASSETTA_SETUP_TOKEN"] = setup_token
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = (
        "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    )
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY_FILE", None)
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    config = load_config()
    backends = replace(
        build_core_defaults(config), metrics_provider=recording_metrics,
    )
    app = create_app(config, backends=backends)
    configure_mcp(config, backends)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        yield client, setup_token, recording_metrics
