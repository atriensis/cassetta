"""Integration tests for the ``cassetta capabilities`` CLI subcommand.

Two things a person typing this command depends on: that it prints every section
of the handshake it was asked for — server and schema version, modes, features,
limits, TTLs — and that it distinguishes its failures by exit code, so a script
can tell "the server is unreachable" (1) from "the server answered and refused"
(2). A command that collapsed both onto 1 would be usable by hand and useless in
a health check.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cassetta.app import create_app
from cassetta.cli import app as cli_app
from cassetta.mcp_server import configure as configure_mcp


def _patch_httpx_for_asgi(
    monkeypatch: pytest.MonkeyPatch,
    app: Any,
    base_url: str,
) -> None:
    """Route httpx.AsyncClient through the ASGI app so the CLI's one-shot
    GET hits our in-process server without a real socket."""
    real_cls = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.ASGITransport(app=app)
        kwargs.setdefault("base_url", base_url)
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


@pytest.fixture
async def running_app(env_setup: None) -> AsyncIterator[Any]:
    app = create_app()
    config = app.state.config
    backends = app.state.backends
    configure_mcp(config, backends)

    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()

    try:
        yield app
    finally:
        stop.set()
        await task


def test_cli_capabilities_happy_path(
    running_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx_for_asgi(monkeypatch, running_app, "http://localhost:16001")
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["capabilities", "--url", "http://localhost:16001"],
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Server version:" in out
    assert "Schema version:" in out
    assert "Supported modes:" in out
    assert "Features:" in out
    assert "Limits:" in out
    assert "TTLs" in out
    # LimitsConfig() defaults: per_file_max is None → renders "unlimited".
    assert "per_file_max" in out
    assert "unlimited" in out
    # Dev-mode server version matches the installed package version.
    assert "0." in out  # any semver with a dot


def test_cli_capabilities_network_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Don't patch httpx — the CLI will actually attempt a connection.
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["capabilities", "--url", "http://127.0.0.1:1"],
    )
    assert result.exit_code == 1, result.output
    # The output combines stdout and stderr in CliRunner by default.
    assert result.output.strip() != ""


def test_cli_capabilities_http_error_exits_2(
    running_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx response maps to exit 2."""

    # Route httpx via an ASGI app that always returns 500.
    async def _fake_app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # pragma: no cover
            return
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"error":"oops"}'})

    real_cls = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.ASGITransport(app=_fake_app)
        kwargs.setdefault("base_url", "http://stub")
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["capabilities", "--url", "http://stub"],
    )
    assert result.exit_code == 2, result.output
