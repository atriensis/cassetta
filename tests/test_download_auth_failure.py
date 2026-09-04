"""Download-path integration tests for ``auth.failure`` emission.

Covers User Story 1 acceptance scenarios AS-6 (expired JWT) and AS-7
(invalid signature). Also pins the rename of
``download_jwt_validation_failed`` → ``auth.failure source=download``
per research.md R4.
"""

from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from typing import ClassVar

import httpx
import pytest

from cassetta.app import create_app
from cassetta.auth.jwt_tokens import sign
from cassetta.config import load_config
from cassetta.defaults.factory import build_core_defaults
from cassetta.mcp_server import configure as configure_mcp

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
_TEST_JWT_KEY = base64.b64decode(_TEST_JWT_KEY_B64)


@dataclass
class MetricCall:
    method: str
    name: str
    value: float | int
    tags: dict[str, str] | None


class RecordingMetrics:
    kind: ClassVar[str] = "test"

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


@pytest.fixture
async def download_app_client(
    storage_dir: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, RecordingMetrics]]:
    metrics = RecordingMetrics()
    os.environ["CASSETTA_SETUP_TOKEN"] = "tok-529-download"
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001"
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY_FILE", None)

    config = load_config()
    backends = replace(
        build_core_defaults(config),
        metrics_provider=metrics,
    )
    app = create_app(config, backends=backends)
    configure_mcp(config, backends)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        yield client, metrics


def _expired_jwt() -> str:
    now = int(time.time())
    return sign(
        {
            "exp": now - 3600,
            "nbf": now - 7200,
            "iat": now - 7200,
            "jti": str(uuid.uuid4()),
            "bundle_path": "inbox/test/foo",
            "bundle_id": uuid.uuid4().hex,
            "recipient": "alice",
            "file_names": ["data.bin"],
        },
        key=_TEST_JWT_KEY,
    )


def _invalid_signature_jwt() -> str:
    now = int(time.time())
    bad_key = b"X" * 32
    return sign(
        {
            "exp": now + 3600,
            "nbf": now,
            "iat": now,
            "jti": str(uuid.uuid4()),
            "bundle_path": "inbox/test/foo",
            "bundle_id": uuid.uuid4().hex,
            "recipient": "alice",
            "file_names": ["data.bin"],
        },
        key=bad_key,
    )


@pytest.mark.asyncio
async def test_expired_jwt_emits_auth_failure_download_jwt_expired(
    download_app_client: tuple[httpx.AsyncClient, RecordingMetrics],
    auth_log_capture: list[logging.LogRecord],
) -> None:
    """An expired download JWT emits source=download, reason=jwt_expired."""
    client, metrics = download_app_client

    token = _expired_jwt()
    response = await client.get(
        "/download/inbox/test/foo/data.bin",
        headers={"Authorization": f"Bearer {token}", "X-Sender": "alice"},
    )

    assert response.status_code == 401
    records = [r for r in auth_log_capture if getattr(r, "event", None) == "auth.failure"]
    assert len(records) == 1
    assert records[0].detail == {  # type: ignore[attr-defined]
        "source": "download",
        "reason": "jwt_expired",
        "identity_hint": None,
    }
    increments = [c for c in metrics.calls if c.name == "cassetta.auth.failures" and c.method == "increment"]
    assert len(increments) == 1
    assert increments[0].tags == {
        "source": "download",
        "reason": "jwt_expired",
    }

    # R4 alignment — old event name no longer appears.
    old = [r for r in auth_log_capture if getattr(r, "event", None) == "download_jwt_validation_failed"]
    assert old == []


@pytest.mark.asyncio
async def test_invalid_signature_jwt_emits_auth_failure_download_jwt_invalid(
    download_app_client: tuple[httpx.AsyncClient, RecordingMetrics],
    auth_log_capture: list[logging.LogRecord],
) -> None:
    """An invalid-signature download JWT emits source=download, reason=jwt_invalid."""
    client, metrics = download_app_client

    token = _invalid_signature_jwt()
    response = await client.get(
        "/download/inbox/test/foo/data.bin",
        headers={"Authorization": f"Bearer {token}", "X-Sender": "alice"},
    )

    assert response.status_code == 401
    records = [r for r in auth_log_capture if getattr(r, "event", None) == "auth.failure"]
    assert len(records) == 1
    assert records[0].detail == {  # type: ignore[attr-defined]
        "source": "download",
        "reason": "jwt_invalid",
        "identity_hint": None,
    }
    increments = [c for c in metrics.calls if c.name == "cassetta.auth.failures" and c.method == "increment"]
    assert len(increments) == 1
    assert increments[0].tags == {
        "source": "download",
        "reason": "jwt_invalid",
    }
