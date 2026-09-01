"""Integration tests for the ``cassetta_capabilities`` MCP tool (Brief 516 US1).

Covers FR-001, FR-003, FR-004, FR-005, FR-006, FR-007, FR-008, FR-009,
FR-010, FR-011, FR-019, SC-003, SC-004, SC-006. Uses the same harness
pattern as ``test_peek_mcp.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import cassetta as _cassetta
from cassetta.app import create_app
from cassetta.mcp_server import configure as configure_mcp

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@dataclass
class _CapturedEvent:
    event: str
    level: int
    identity_label: str | None
    detail: Any


class _EventHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[_CapturedEvent] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        self.records.append(
            _CapturedEvent(
                event=str(getattr(record, "event", None) or record.getMessage()),
                level=record.levelno,
                identity_label=getattr(record, "identity_label", None),
                detail=getattr(record, "detail", None),
            )
        )


@pytest.fixture
def cap_storage_dir() -> Any:  # yields str
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


@pytest.fixture
def cap_env(cap_storage_dir: str) -> Any:  # yields None
    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = cap_storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ["CASSETTA_PER_FILE_MAX"] = "1024"
    # Only one cap set; the rest should come from defaults / null.
    os.environ.pop("CASSETTA_PER_BUNDLE_TOTAL_MAX", None)
    os.environ.pop("CASSETTA_PER_BUNDLE_FILE_COUNT_MAX", None)
    os.environ.pop("CASSETTA_KEYS_FILE", None)
    yield
    os.environ.pop("CASSETTA_PER_FILE_MAX", None)


@pytest.fixture
async def mcp_cap(cap_env: None, cap_storage_dir: str) -> AsyncIterator[tuple]:
    app = create_app()
    config = app.state.config
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
        yield app, client, backend

    stop.set()
    await task


@pytest.fixture
def log_capture() -> Any:
    handler = _EventHandler()
    logger = logging.getLogger("cassetta")
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


async def _post(
    client: httpx.AsyncClient,
    body: dict,
    sid: str = "",
) -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    return await client.post("/mcp/", json=body, headers=headers)


async def _init(client: httpx.AsyncClient) -> str:
    resp = await _post(
        client,
        _jsonrpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "cap-test", "version": "1.0.0"},
            },
        ),
    )
    assert resp.status_code == 200
    return resp.headers.get("mcp-session-id", "")


async def _call(
    client: httpx.AsyncClient,
    name: str,
    args: dict,
    sid: str = "",
) -> dict:
    body = _jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2)
    resp = await _post(client, body, sid)
    assert resp.status_code == 200
    return resp.json()["result"]


def _snapshot_dir(root: str) -> str:
    items: list[str] = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            stat = path.stat()
            items.append(f"{path.relative_to(root)}|{stat.st_size}")
        else:
            items.append(f"{path.relative_to(root)}/")
    return hashlib.sha256("\n".join(items).encode()).hexdigest()


@pytest.mark.asyncio
async def test_mcp_capabilities_matches_schema_and_config(
    mcp_cap: tuple,
    log_capture: _EventHandler,
) -> None:
    _app, client, _backend = mcp_cap
    sid = await _init(client)

    result = await _call(client, "cassetta_capabilities", {}, sid)
    assert result.get("isError") is not True
    doc = json.loads(result["content"][0]["text"])

    # FR-001 / FR-019: six top-level keys, schema_version == 1.
    assert set(doc.keys()) == {
        "schema_version",
        "server_version",
        "supported_modes",
        "limits",
        "ttls",
        "features",
    }
    assert doc["schema_version"] == 1

    # FR-006: server_version matches package version.
    assert doc["server_version"] == _cassetta.__version__

    # FR-007 / FR-008: exact lists.
    assert doc["supported_modes"] == ["inline", "batch", "reference"]
    assert doc["features"] == ["peek", "batch_upload", "reference_download", "rest_send_init"]

    # FR-003: CASSETTA_PER_FILE_MAX=1024 propagated; others null or default.
    assert doc["limits"]["per_file_max"] == 1024
    assert doc["limits"]["per_bundle_total_max"] is None
    # per_bundle_file_count_max and max_inline_size have LimitsConfig defaults.
    assert doc["limits"]["per_bundle_file_count_max"] == 25
    assert doc["limits"]["max_inline_size"] == 102400

    # FR-004: ttls block has four integer fields.
    for key in (
        "upload_token_ttl",
        "download_claim_ttl",
        "passive_gc_min_age",
        "passive_gc_interval",
    ):
        assert isinstance(doc["ttls"][key], int)


@pytest.mark.asyncio
async def test_mcp_capabilities_100_calls_identical_no_info_logs_no_side_effects_p99(
    mcp_cap: tuple,
    log_capture: _EventHandler,
    cap_storage_dir: str,
) -> None:
    """FR-009, FR-011, SC-003, SC-004, SC-006 — all rolled into one run."""
    _app, client, _backend = mcp_cap
    sid = await _init(client)

    before_hash = _snapshot_dir(cap_storage_dir)
    durations_ns: list[int] = []
    first_text: str | None = None

    for _ in range(100):
        t0 = time.perf_counter_ns()
        result = await _call(client, "cassetta_capabilities", {}, sid)
        durations_ns.append(time.perf_counter_ns() - t0)
        text = result["content"][0]["text"]
        if first_text is None:
            first_text = text
        # FR-009: byte-identical across 100 calls.
        assert text == first_text

    # SC-004: no storage touch.
    after_hash = _snapshot_dir(cap_storage_dir)
    assert after_hash == before_hash

    # SC-006 + FR-011: zero INFO+ logs for capabilities.queried calls.
    info_plus = [r for r in log_capture.records if r.level >= logging.INFO]
    # Filter to events originating from our 100 calls — any capabilities.queried
    # must be DEBUG; other unrelated INFO events (e.g. startup) don't count.
    queried_over_info = [r for r in info_plus if r.event == "capabilities.queried"]
    assert queried_over_info == []

    # And the DEBUG events were all emitted.
    queried = [r for r in log_capture.records if r.event == "capabilities.queried"]
    assert len(queried) == 100
    for r in queried:
        assert r.detail == {"via": "mcp"}
        assert r.level == logging.DEBUG

    # SC-003: p99 < 50 ms.
    durations_ns.sort()
    p99 = durations_ns[int(len(durations_ns) * 0.99) - 1]
    p99_ms = p99 / 1_000_000
    assert p99_ms < 50, f"p99 latency {p99_ms:.2f} ms exceeds 50 ms budget"


@pytest.mark.asyncio
async def test_mcp_capabilities_all_unlimited(
    cap_env: None,
    cap_storage_dir: str,
    log_capture: _EventHandler,
) -> None:
    """FR-003 — explicit null caps surface as null."""
    # Override the per-file cap the fixture set; also null the defaults.
    os.environ["CASSETTA_PER_FILE_MAX"] = ""
    os.environ["CASSETTA_PER_BUNDLE_TOTAL_MAX"] = ""
    os.environ["CASSETTA_PER_BUNDLE_FILE_COUNT_MAX"] = ""
    os.environ["CASSETTA_MAX_INLINE_SIZE"] = ""
    try:
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
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost:16001",
            ) as client:
                sid = await _init(client)
                result = await _call(client, "cassetta_capabilities", {}, sid)
                doc = json.loads(result["content"][0]["text"])
                assert doc["limits"] == {
                    "per_file_max": None,
                    "per_bundle_total_max": None,
                    "per_bundle_file_count_max": None,
                    "max_inline_size": None,
                }
        finally:
            stop.set()
            await task
    finally:
        for key in (
            "CASSETTA_PER_FILE_MAX",
            "CASSETTA_PER_BUNDLE_TOTAL_MAX",
            "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
            "CASSETTA_MAX_INLINE_SIZE",
        ):
            os.environ.pop(key, None)
