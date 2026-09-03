"""Dev-mode visibility tests.

Covers:
- AS-1, AS-2: lifespan ``config_loaded.dev_mode`` field + WARNING-level
  ``dev_mode_enabled`` event when dev-mode is active.
- AS-3, AS-4, AS-5: ``/health`` body always includes ``dev_mode`` field
  on both 200 and 503 paths.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest

from cassetta.app import create_app, lifespan
from cassetta.config import load_config
from cassetta.defaults.factory import build_core_defaults

_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


def _set_common_env() -> None:
    os.environ["CASSETTA_STORAGE_PATH"] = tempfile.mkdtemp()
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY_FILE", None)


def _attach_capture(
    logger_name: str,
) -> tuple[
    list[logging.LogRecord],
    logging.Handler,
]:
    """Attach an in-memory handler to ``logger_name`` and return both.

    Caller is responsible for ``logger.removeHandler(handler)`` cleanup.
    Used in lifespan tests where ``create_app`` calls
    ``configure_logging`` which clears any previously-attached handler.
    """
    captured: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Handler()
    handler.setLevel(logging.DEBUG)
    target = logging.getLogger(logger_name)
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    return captured, handler


@asynccontextmanager
async def _run_lifespan(app: object) -> AsyncIterator[None]:
    """Drive the FastAPI lifespan startup + shutdown around a yield."""
    async with lifespan(app):  # type: ignore[arg-type]
        yield


@pytest.mark.asyncio
async def test_config_loaded_carries_dev_mode_false_in_auth_mode() -> None:
    """US2 AS-1: auth-enabled boot → config_loaded.dev_mode=false, no
    dev_mode_enabled event."""
    _set_common_env()
    os.environ["CASSETTA_SETUP_TOKEN"] = "test-tok-529-us2"

    config = load_config()
    backends = build_core_defaults(config)
    app = create_app(config, backends=backends)

    captured, handler = _attach_capture("cassetta")
    try:
        async with _run_lifespan(app):
            pass
    finally:
        logging.getLogger("cassetta").removeHandler(handler)

    config_loaded = [r for r in captured if getattr(r, "event", None) == "config_loaded"]
    assert len(config_loaded) == 1
    assert config_loaded[0].detail["dev_mode"] is False  # type: ignore[attr-defined]

    enabled = [r for r in captured if getattr(r, "event", None) == "dev_mode_enabled"]
    assert enabled == []


@pytest.mark.asyncio
async def test_config_loaded_carries_dev_mode_true_in_dev_mode() -> None:
    """US2 AS-2: empty setup token → config_loaded.dev_mode=true and one
    WARNING-level dev_mode_enabled event."""
    _set_common_env()
    os.environ["CASSETTA_SETUP_TOKEN"] = ""

    config = load_config()
    backends = build_core_defaults(config)
    app = create_app(config, backends=backends)

    captured, handler = _attach_capture("cassetta")
    try:
        async with _run_lifespan(app):
            pass
    finally:
        logging.getLogger("cassetta").removeHandler(handler)

    config_loaded = [r for r in captured if getattr(r, "event", None) == "config_loaded"]
    assert len(config_loaded) == 1
    assert config_loaded[0].detail["dev_mode"] is True  # type: ignore[attr-defined]

    enabled = [r for r in captured if getattr(r, "event", None) == "dev_mode_enabled"]
    assert len(enabled) == 1
    assert enabled[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_health_includes_dev_mode_false_in_auth_mode() -> None:
    """US2 AS-3: /health 200 body contains dev_mode=false."""
    _set_common_env()
    os.environ["CASSETTA_SETUP_TOKEN"] = "test-tok-529-health"

    config = load_config()
    backends = build_core_defaults(config)
    app = create_app(config, backends=backends)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dev_mode": False}


@pytest.mark.asyncio
async def test_health_includes_dev_mode_true_in_dev_mode() -> None:
    """US2 AS-4: /health 200 body contains dev_mode=true in dev mode."""
    _set_common_env()
    os.environ["CASSETTA_SETUP_TOKEN"] = ""

    config = load_config()
    backends = build_core_defaults(config)
    app = create_app(config, backends=backends)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "dev_mode": True}


class _UnhealthyKeyStore:
    """Stub key store whose ``is_healthy`` always returns False."""

    kind = "test-unhealthy"

    def is_healthy(self) -> bool:
        return False

    async def validate(self, _token: str) -> None:
        return None


@pytest.mark.asyncio
async def test_health_503_includes_dev_mode_field() -> None:
    """US2 AS-5: 503 body still contains dev_mode alongside status + reason."""
    _set_common_env()
    os.environ["CASSETTA_SETUP_TOKEN"] = "test-tok-529-503"

    config = load_config()
    backends = replace(
        build_core_defaults(config),
        key_store=_UnhealthyKeyStore(),
    )
    app = create_app(config, backends=backends)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["reason"] == "key_store_unreachable"
    assert body["dev_mode"] is False
