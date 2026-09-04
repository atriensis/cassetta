"""Happy-path integration tests for LimitsPolicy-gated uploads."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.app import create_app
from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.config import AppConfig, LimitsConfig, load_config
from cassetta.defaults.default_limits import CoreLimitsPolicy
from cassetta.mcp_server import configure as configure_mcp
from cassetta.mcp_server import set_sender_label

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


@pytest.fixture
def policy_storage_dir() -> str:
    return tempfile.mkdtemp()


_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"


@pytest.fixture
def policy_env(policy_storage_dir: str) -> None:
    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = policy_storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = _TEST_JWT_KEY_B64
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY_FILE", None)
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    for var in (
        "CASSETTA_PER_FILE_MAX",
        "CASSETTA_PER_BUNDLE_TOTAL_MAX",
        "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
        "CASSETTA_MAX_INLINE_SIZE",
        "CASSETTA_MAX_FILE_SIZE",
    ):
        os.environ.pop(var, None)


@pytest.fixture
async def policy_client(
    policy_env: None,
    policy_storage_dir: str,
    make_backends,
) -> AsyncIterator[tuple[httpx.AsyncClient, FilesystemBackend]]:
    limits = LimitsConfig(
        per_file_max=1024,
        per_bundle_total_max=8192,
        per_bundle_file_count_max=5,
        max_inline_size=2048,
    )
    policy = CoreLimitsPolicy(limits)

    config = load_config()
    config = AppConfig(
        setup_token=config.setup_token,
        dev_mode=config.dev_mode,
        storage_path=config.storage_path,
        keys_file=config.keys_file,
        default_ttl=config.default_ttl,
        allowed_path_chars=config.allowed_path_chars,
        mcp_allowed_hosts=config.mcp_allowed_hosts,
        invite_ttl_seconds=config.invite_ttl_seconds,
        log_format=config.log_format,
        limits=limits,
    )
    app = create_app(
        config,
        backends=make_backends(config, limits_policy=policy),
    )
    backends = app.state.backends
    backend = backends.backend
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
        yield client, backend

    stop.set()
    await task


async def _init_mcp(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/mcp/",
        json=_jsonrpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "policy-test", "version": "1.0.0"},
            },
        ),
        headers=MCP_HEADERS,
    )
    return resp.headers.get("mcp-session-id", "")


async def _call_mcp(
    client: httpx.AsyncClient,
    sid: str,
    name: str,
    args: dict,
) -> dict:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    resp = await client.post(
        "/mcp/",
        json=_jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2),
        headers=headers,
    )
    return resp.json()["result"]


@pytest.mark.asyncio
async def test_mcp_put_within_caps_succeeds(
    policy_client: tuple[httpx.AsyncClient, FilesystemBackend],
) -> None:
    client, _ = policy_client
    sid = await _init_mcp(client)

    put_result = await _call_mcp(
        client,
        sid,
        "cassetta_put",
        {"path": "foo", "content": "x" * 500},
    )
    assert put_result.get("isError") is not True

    get_result = await _call_mcp(client, sid, "cassetta_get", {"path": "foo"})
    envelope = json.loads(get_result["content"][0]["text"])
    assert envelope["mode"] == "inline"
    assert envelope["files"][0]["content"] == "x" * 500


@pytest.mark.asyncio
async def test_mcp_send_init_within_caps_succeeds(
    policy_client: tuple[httpx.AsyncClient, FilesystemBackend],
) -> None:
    """``cassetta_send_init`` is the send entry point.

    A manifest that fits within the policy caps must yield the
    discriminated inline response (``mode == "inline"`` with an
    ``inline_token`` + ``expires_at``).
    """
    client, _ = policy_client
    sid = await _init_mcp(client)

    await client.post("/setup", json={"host": "test", "project": "alice"})
    set_sender_label("test:bob")

    result = await _call_mcp(
        client,
        sid,
        "cassetta_send_init",
        {
            "to": "test:alice",
            "path": "bar",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "bar", "size": 500}],
            },
        },
    )
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    # With per_file_max=1024, max_inline_size=2048 and 500-byte file,
    # this fits within caps and must be inline.
    assert payload["mode"] == "inline"
    assert payload["bundle_id"]
    assert payload["inline_token"]
    assert payload["expires_at"]


@pytest.mark.asyncio
async def test_rest_multipart_within_caps_succeeds(
    policy_client: tuple[httpx.AsyncClient, FilesystemBackend],
) -> None:
    client, _ = policy_client
    files = [
        ("files", ("a.txt", b"a" * 500)),
        ("files", ("b.txt", b"b" * 500)),
        ("files", ("c.txt", b"c" * 500)),
    ]
    resp = await client.put("/files/proj", files=files)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_rest_multipart_too_many_files_rejected(
    policy_client: tuple[httpx.AsyncClient, FilesystemBackend],
) -> None:
    client, _ = policy_client
    files = [("files", (f"f{i}.txt", b"x" * 100)) for i in range(6)]
    resp = await client.put("/files/many", files=files)
    assert resp.status_code == 413
    assert resp.json()["constraint"] == "per_bundle_file_count_max"


@pytest.mark.asyncio
async def test_rest_multipart_total_over_inline_is_batch_required(
    policy_client: tuple[httpx.AsyncClient, FilesystemBackend],
) -> None:
    client, _ = policy_client
    # 5 files * 500 bytes = 2500 bytes total, caps allow it,
    # max_inline_size=2048 < 2500 → batch_required
    files = [("files", (f"f{i}.txt", b"x" * 500)) for i in range(5)]
    resp = await client.put("/files/overflow", files=files)
    assert resp.status_code == 422
    assert resp.json()["error"] == "batch_required"
