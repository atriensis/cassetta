"""T037 (US4) — ``cassetta download`` subcommand round-trip + error paths.

The CLI reads a reference envelope from stdin (or a file path), iterates
``files[].url`` with the ``download_token`` in ``Authorization``, and
streams each file into ``--out/<name>``. Exit 0 on full success; non-
zero on any HTTP/network failure with the server body on stderr.

Integration strategy: the CLI uses ``httpx.Client``, so we patch its
transport to the ASGI transport wrapped around the running app.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest import mock

import httpx
import jwt as _jwt
import pytest
from typer.testing import CliRunner

from cassetta.app import create_app
from cassetta.auth import jwt_tokens
from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.cli import app as cli_app
from cassetta.config import load_config
from cassetta.mcp_server import configure as configure_mcp

from .conftest import SETUP_TOKEN, CoreHelpers, seed_inbox_bundle


async def _boot(storage_dir: str) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:  # type: ignore[name-defined]
    os.environ["CASSETTA_SETUP_TOKEN"] = SETUP_TOKEN
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = (
        "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    )
    os.environ["CASSETTA_JWT_KEY"] = (
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    )
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ["CASSETTA_MAX_INLINE_SIZE"] = "32"
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_KEYS_FILE", None)

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

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        yield client, app

    stop.set()
    await task


@pytest.mark.asyncio
async def test_cli_download_happy_path() -> None:
    storage_dir = tempfile.mkdtemp()
    h = CoreHelpers()

    async for client, app in _boot(storage_dir):
        sender_key = await h.setup_agent(client, "bob", "main")
        alice_key = await h.create_key(client, sender_key, "alice", "main")

        backend = FilesystemBackend(root_path=storage_dir)
        files = [
            ("notes.md", b"# Hello\nthis is a test\n" * 20),
            ("src/lib.py", b"print('brief 515 CLI')\n" * 30),
        ]
        await seed_inbox_bundle(
            backend, "alice:main", "big-drop",
            files=files, sender="bob",
        )
        sid = await h.mcp_init(client, api_key=alice_key)
        pick = await h.mcp_call(
            client, "cassetta_pick", {"path": "big-drop"},
            sid=sid, api_key=alice_key,
        )
        envelope_text = pick["content"][0]["text"]
        envelope = json.loads(envelope_text)
        assert envelope["mode"] == "reference"

        # Run the CLI against an ASGI-transport httpx.Client.
        out_dir = Path(storage_dir) / "dl"
        _run_cli_with_asgi(app, envelope_text, out_dir, expected_exit=0)

        # Assert files landed with correct bytes.
        for name, data in files:
            assert (out_dir / name).read_bytes() == data


@pytest.mark.asyncio
async def test_cli_download_expired_jwt_fails() -> None:
    storage_dir = tempfile.mkdtemp()
    h = CoreHelpers()

    async for client, app in _boot(storage_dir):
        sender_key = await h.setup_agent(client, "bob", "main")
        alice_key = await h.create_key(client, sender_key, "alice", "main")

        backend = FilesystemBackend(root_path=storage_dir)
        await seed_inbox_bundle(
            backend, "alice:main", "short-lived",
            files=[("a.bin", b"a" * 200)], sender="bob",
        )
        sid = await h.mcp_init(client, api_key=alice_key)
        pick = await h.mcp_call(
            client, "cassetta_pick", {"path": "short-lived"},
            sid=sid, api_key=alice_key,
        )
        envelope = json.loads(pick["content"][0]["text"])

        # Replace the envelope's download_token with an expired one.
        config = load_config()
        now = int(time.time())
        expired_claims = _jwt.decode(
            envelope["download_token"], options={"verify_signature": False},
        )
        expired_claims["iat"] = now - 10_000
        expired_claims["nbf"] = now - 10_000
        expired_claims["exp"] = now - 5_000
        expired_token = jwt_tokens.sign(
            expired_claims, key=config.jwt_primary_key,
        )
        envelope["download_token"] = expired_token

        out_dir = Path(storage_dir) / "dl-expired"
        result = _run_cli_with_asgi(
            app, json.dumps(envelope), out_dir, expected_exit=None,
        )
        assert result.exit_code != 0, result.output
        combined = (result.output or "") + (getattr(result, "stderr", "") or "")
        # The body surfaced to stderr includes the JWT error reason.
        assert "expired" in combined.lower()


@pytest.mark.asyncio
async def test_cli_download_not_reference_mode_exits_nonzero() -> None:
    """A ``mode: inline`` envelope isn't handled by this subcommand."""
    storage_dir = tempfile.mkdtemp()
    os.environ.pop("CASSETTA_MAX_INLINE_SIZE", None)
    runner = CliRunner()
    inline = {
        "mode": "inline", "bundle": {"bundle_id": "x"},
        "files": [{"name": "foo.md", "content": "hi", "encoding": "utf8"}],
    }
    result = runner.invoke(
        cli_app,
        ["download", "--manifest-json", "-", "--out", str(storage_dir)],
        input=json.dumps(inline),
    )
    assert result.exit_code != 0


def _run_cli_with_asgi(
    app: Any, envelope_json: str, out_dir: Path,  # type: ignore[name-defined]
    expected_exit: int | None,
) -> Any:  # type: ignore[misc]
    """Invoke the ``cassetta download`` CLI with httpx.AsyncClient wired to ASGI."""
    import httpx as _httpx

    real_Client = _httpx.AsyncClient

    def _patched_client(*args, **kwargs):  # noqa: ANN001, ANN002, ANN202
        kwargs["transport"] = _httpx.ASGITransport(app=app)
        kwargs.setdefault("base_url", "http://localhost:16001")
        return real_Client(*args, **kwargs)

    runner = CliRunner()
    with mock.patch("cassetta.cli.download.httpx.AsyncClient", _patched_client):
        result = runner.invoke(
            cli_app,
            ["download", "--manifest-json", "-", "--out", str(out_dir)],
            input=envelope_json,
        )
    if expected_exit is not None:
        assert result.exit_code == expected_exit, (
            f"expected exit {expected_exit}, got {result.exit_code}:\n"
            f"stdout: {result.output}\nexception: {result.exception!r}"
        )
    return result


# Forward-declared Any for type annotations under __future__.
from typing import Any  # noqa: E402
