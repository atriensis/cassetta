"""REST-path integration tests for ``auth.failure`` emission.

Covers the failure paths and the no-raw-credential-leakage rule, using a
real auth-enabled FastAPI ``TestClient`` with a recording
``MetricsProvider``.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from .conftest import RecordingMetricsProvider


def _auth_failure_records(
    captured: list[logging.LogRecord],
) -> list[logging.LogRecord]:
    return [r for r in captured if getattr(r, "event", None) == "auth.failure"]


@pytest.mark.asyncio
async def test_missing_authorization_emits_auth_failure_rest_missing_bearer(
    auth_metrics_client: tuple[
        httpx.AsyncClient,
        str,
        RecordingMetricsProvider,
    ],
    auth_log_capture: list[logging.LogRecord],
) -> None:
    """US1 AS-1: REST 401 with no Authorization header."""
    client, _setup_token, metrics = auth_metrics_client

    response = await client.get("/files/inbox/anything/foo")

    assert response.status_code == 401
    records = _auth_failure_records(auth_log_capture)
    assert len(records) == 1
    assert records[0].detail == {  # type: ignore[attr-defined]
        "source": "rest",
        "reason": "missing_bearer",
        "identity_hint": None,
    }
    increments = metrics.find("cassetta.auth.failures", method="increment")
    assert len(increments) == 1
    assert increments[0].tags == {
        "source": "rest",
        "reason": "missing_bearer",
    }


@pytest.mark.asyncio
async def test_invalid_bearer_emits_auth_failure_rest_invalid_key(
    auth_metrics_client: tuple[
        httpx.AsyncClient,
        str,
        RecordingMetricsProvider,
    ],
    auth_log_capture: list[logging.LogRecord],
) -> None:
    """US1 AS-2: REST 401 with invalid bearer; identity_hint is first 12 chars."""
    client, _setup_token, metrics = auth_metrics_client

    bearer = "ZZZZ-NEVER-EXISTED-1234567890"
    response = await client.get(
        "/files/inbox/anything/foo",
        headers={"Authorization": f"Bearer {bearer}"},
    )

    assert response.status_code == 401
    records = _auth_failure_records(auth_log_capture)
    assert len(records) == 1
    detail = records[0].detail  # type: ignore[attr-defined]
    assert detail["source"] == "rest"
    assert detail["reason"] == "invalid_key"
    assert detail["identity_hint"] == bearer[:12]
    assert len(detail["identity_hint"]) <= 12

    # No record contains the raw bearer.
    for r in auth_log_capture:
        rendered = f"{r.getMessage()}|{getattr(r, 'detail', '')}|{getattr(r, 'identity_label', '')}"
        assert bearer not in rendered

    increments = metrics.find("cassetta.auth.failures", method="increment")
    assert len(increments) == 1
    assert increments[0].tags == {
        "source": "rest",
        "reason": "invalid_key",
    }


@pytest.mark.asyncio
async def test_invalid_setup_token_emits_auth_failure_rest_invalid_setup_token(
    auth_metrics_client: tuple[
        httpx.AsyncClient,
        str,
        RecordingMetricsProvider,
    ],
    auth_log_capture: list[logging.LogRecord],
) -> None:
    """US1 AS-3: REST 401/403 with bad X-Setup-Token."""
    client, _setup_token, metrics = auth_metrics_client

    response = await client.post(
        "/setup",
        json={"host": "test", "project": "test"},
        headers={"X-Setup-Token": "wrong-token"},
    )

    assert response.status_code in (401, 403)
    records = _auth_failure_records(auth_log_capture)
    matching = [
        r
        for r in records
        if r.detail.get("reason") == "invalid_setup_token"  # type: ignore[attr-defined]
    ]
    assert len(matching) == 1
    assert matching[0].detail == {  # type: ignore[attr-defined]
        "source": "rest",
        "reason": "invalid_setup_token",
        "identity_hint": None,
    }
    increments = [
        c
        for c in metrics.find("cassetta.auth.failures", method="increment")
        if c.tags == {"source": "rest", "reason": "invalid_setup_token"}
    ]
    assert len(increments) == 1


@pytest.mark.asyncio
async def test_successful_auth_emits_no_auth_failure(
    auth_metrics_client: tuple[
        httpx.AsyncClient,
        str,
        RecordingMetricsProvider,
    ],
    auth_log_capture: list[logging.LogRecord],
) -> None:
    """The success path emits ZERO auth.failure events."""
    client, setup_token, metrics = auth_metrics_client

    response = await client.post(
        "/setup",
        json={"host": "test", "project": "test"},
        headers={"X-Setup-Token": setup_token},
    )

    assert response.status_code == 201
    records = _auth_failure_records(auth_log_capture)
    assert records == []
    assert metrics.find("cassetta.auth.failures", method="increment") == []
