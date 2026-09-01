"""Tests for MCP tool operations — protocol-level tests."""

import asyncio
import io
import json
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from cassetta.app import create_app
from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.mcp_server import configure as configure_mcp

# Standard MCP headers for Streamable HTTP
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
def mcp_storage_dir() -> str:
    return tempfile.mkdtemp()


@pytest.fixture
def mcp_env(mcp_storage_dir: str) -> None:
    """Dev mode environment for MCP tool tests."""
    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = mcp_storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MAX_FILE_SIZE"] = "1048576"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = (
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    )
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"


@pytest.fixture
async def mcp_app(mcp_env: None, mcp_storage_dir: str) -> AsyncIterator[tuple]:
    """Return (app, httpx client, backend) for MCP testing."""
    app = create_app()
    config = app.state.config
    backends = app.state.backends
    backend = backends.backend
    configure_mcp(config, backends)

    # Start session manager in a background task to avoid anyio scope issues
    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run_manager() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run_manager())
    await started.wait()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as client:
        yield app, client, backend

    stop.set()
    await task


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _init_msg() -> dict:
    return _jsonrpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0.0"},
    })


async def _post(client: httpx.AsyncClient, body: dict, sid: str = "") -> httpx.Response:
    headers = dict(MCP_HEADERS)
    if sid:
        headers["mcp-session-id"] = sid
    return await client.post("/mcp/", json=body, headers=headers)


async def _init(client: httpx.AsyncClient) -> str:
    """Initialize MCP session, return session_id."""
    resp = await _post(client, _init_msg())
    assert resp.status_code == 200, f"Init failed: {resp.status_code} {resp.text}"
    return resp.headers.get("mcp-session-id", "")


async def _call(
    client: httpx.AsyncClient, name: str, args: dict, sid: str = "",
) -> dict:
    """Call a tool, return result dict."""
    body = _jsonrpc("tools/call", {"name": name, "arguments": args}, req_id=2)
    resp = await _post(client, body, sid)
    assert resp.status_code == 200, f"Call failed: {resp.status_code} {resp.text}"
    return resp.json()["result"]


async def _put_bundle(
    backend: FilesystemBackend, path: str, content: bytes,
    *, namespace: str = "store", sender: str | None = None,
) -> None:
    """Seed a committed bundle in the given namespace (default: store/)."""
    name = path.split("/")[-1]
    bundle_path = f"{namespace}/{path}"
    writer = await backend.open_bundle_write(bundle_path)
    try:
        await writer.write_file(name, io.BytesIO(content))
        meta = {
            "schema_version": 1,
            "bundle_id": uuid.uuid4().hex,
            "sender": sender,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": 1,
            "files": [
                {"name": name, "size": len(content), "mime": "application/octet-stream"},
            ],
        }
        await writer.commit(meta)
    except Exception:
        await writer.abort()
        raise


async def _put_tar(backend: FilesystemBackend, path: str, content: bytes) -> None:
    """Legacy alias kept to keep test call-sites unchanged."""
    await _put_bundle(backend, path, content)


# ============================================================
# US5: Tool Discovery
# ============================================================


class TestToolDiscovery:

    @pytest.mark.asyncio
    async def test_discover_all_tools(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app
        sid = await _init(client)

        resp = await _post(client, _jsonrpc("tools/list", {}, req_id=2), sid)
        assert resp.status_code == 200
        tool_names = [t["name"] for t in resp.json()["result"]["tools"]]
        assert sorted(tool_names) == [
            "cassetta_agents", "cassetta_broadcast", "cassetta_capabilities",
            "cassetta_delete", "cassetta_get", "cassetta_inbox",
            "cassetta_list", "cassetta_peek", "cassetta_pick",
            "cassetta_put", "cassetta_send_init", "cassetta_send_inline",
        ]
        assert "cassetta_send" not in tool_names

    @pytest.mark.asyncio
    async def test_tool_schemas(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app
        sid = await _init(client)

        resp = await _post(client, _jsonrpc("tools/list", {}, req_id=2), sid)
        tools = {t["name"]: t for t in resp.json()["result"]["tools"]}

        assert "path" in tools["cassetta_put"]["inputSchema"]["properties"]
        assert "content" in tools["cassetta_put"]["inputSchema"]["properties"]
        assert "path" in tools["cassetta_get"]["inputSchema"]["properties"]
        assert "prefix" in tools["cassetta_list"]["inputSchema"]["properties"]
        # Inbox tool schemas (Brief 514: two-phase send replaces cassetta_send)
        assert "to" in tools["cassetta_send_init"]["inputSchema"]["properties"]
        assert "manifest" in tools["cassetta_send_init"]["inputSchema"]["properties"]
        assert "token" in tools["cassetta_send_inline"]["inputSchema"]["properties"]
        assert "agent" in tools["cassetta_inbox"]["inputSchema"]["properties"]
        assert "path" in tools["cassetta_pick"]["inputSchema"]["properties"]

    @pytest.mark.asyncio
    async def test_send_tools_expose_typed_schemas(self, mcp_app: tuple) -> None:
        """Brief 541: send_init/send_inline advertise typed payloads, not open objects."""
        _app, client, _backend = mcp_app
        sid = await _init(client)

        resp = await _post(client, _jsonrpc("tools/list", {}, req_id=2), sid)
        tools = {t["name"]: t for t in resp.json()["result"]["tools"]}

        def _resolve(schema: dict, node: dict) -> dict:
            if "allOf" in node and len(node["allOf"]) == 1:
                node = node["allOf"][0]
            ref = node.get("$ref")
            if ref:
                return schema["$defs"][ref.split("/")[-1]]
            return node

        # send_init.manifest → SendManifest → files[] → SendManifestFile{name, size}
        init_schema = tools["cassetta_send_init"]["inputSchema"]
        manifest = _resolve(init_schema, init_schema["properties"]["manifest"])
        mfile = _resolve(init_schema, manifest["properties"]["files"]["items"])
        assert "name" in mfile["properties"]
        assert "size" in mfile["properties"]
        assert set(mfile["required"]) >= {"name", "size"}
        # FR-002: the size field documents the decoded-byte semantics.
        assert "decod" in (mfile["properties"]["size"].get("description") or "").lower()
        # Not an open/untyped object.
        assert mfile.get("additionalProperties") is not True

        # send_inline.files → SendInlineFile{name, content, encoding-enum}
        inline_schema = tools["cassetta_send_inline"]["inputSchema"]
        ifile = _resolve(inline_schema, inline_schema["properties"]["files"]["items"])
        assert set(ifile["required"]) >= {"name", "content", "encoding"}
        assert ifile["properties"]["encoding"]["enum"] == ["base64", "utf8"]


# ============================================================
# US1: cassetta_put
# ============================================================


class TestCassettaPut:

    @pytest.mark.asyncio
    async def test_put_stores_file(self, mcp_app: tuple) -> None:
        _app, client, backend = mcp_app
        sid = await _init(client)

        result = await _call(client, "cassetta_put", {
            "path": "test/hello.txt", "content": "Hello World",
        }, sid)
        assert result.get("isError") is not True
        text = result["content"][0]["text"]
        assert "test/hello.txt" in text
        assert "11" in text

        # Verify bundle stored via new bundle Protocol
        meta = await backend.read_bundle_meta("store/test/hello.txt")
        assert meta["files"][0]["name"] == "hello.txt"
        handle = await backend.open_bundle_file_read("store/test/hello.txt", "hello.txt")
        try:
            assert handle.read() == b"Hello World"
        finally:
            handle.close()

    @pytest.mark.asyncio
    async def test_put_overwrites_existing(self, mcp_app: tuple) -> None:
        _app, client, backend = mcp_app
        await _put_tar(backend, "overwrite.txt", b"old content")
        sid = await _init(client)

        result = await _call(client, "cassetta_put", {
            "path": "overwrite.txt", "content": "new content",
        }, sid)
        assert result.get("isError") is not True

        handle = await backend.open_bundle_file_read("store/overwrite.txt", "overwrite.txt")
        try:
            assert handle.read() == b"new content"
        finally:
            handle.close()

    @pytest.mark.asyncio
    async def test_put_rejects_path_traversal(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app
        sid = await _init(client)

        result = await _call(client, "cassetta_put", {
            "path": "../etc/passwd", "content": "malicious",
        }, sid)
        assert result["isError"] is True

    @pytest.mark.asyncio
    async def test_put_rejects_oversized_content(self, mcp_app: tuple) -> None:
        """Payload above max_inline_size surfaces as MCP ``batch_required:`` error."""
        _app, client, _backend = mcp_app
        sid = await _init(client)

        result = await _call(client, "cassetta_put", {
            "path": "big.txt", "content": "x" * (100 * 1024 + 1),
        }, sid)
        assert result["isError"] is True
        assert result["content"][0]["text"].lower().startswith(
            "error executing tool cassetta_put: batch_required:",
        )


# ============================================================
# US2: cassetta_get
# ============================================================


class TestCassettaGet:

    @pytest.mark.asyncio
    async def test_get_returns_content(self, mcp_app: tuple) -> None:
        _app, client, backend = mcp_app
        await _put_tar(backend, "data/config.json", b'{"key": "value"}')
        sid = await _init(client)

        result = await _call(client, "cassetta_get", {"path": "data/config.json"}, sid)
        assert result.get("isError") is not True
        envelope = json.loads(result["content"][0]["text"])
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == '{"key": "value"}'

    @pytest.mark.asyncio
    async def test_get_missing_file_returns_error(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app
        sid = await _init(client)

        result = await _call(client, "cassetta_get", {"path": "missing/file.txt"}, sid)
        assert result["isError"] is True
        assert "not found" in result["content"][0]["text"].lower()

    @pytest.mark.asyncio
    async def test_get_expired_file_returns_error(
        self, mcp_storage_dir: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Brief 539: set env via monkeypatch (auto-restored) and provide
        # CASSETTA_MCP_ALLOWED_HOSTS ourselves — this test used to free-ride on
        # a value leaked by an earlier test, so it 421'd under isolation /
        # natural order once the env-guard stopped the leak (Principle IX).
        monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
        monkeypatch.setenv("CASSETTA_STORAGE_PATH", mcp_storage_dir)
        monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "1")
        monkeypatch.setenv(
            "CASSETTA_MCP_ALLOWED_HOSTS",
            "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001",
        )

        app = create_app()
        config = app.state.config
        backends = app.state.backends
        backend = backends.backend
        configure_mcp(config, backends)

        await _put_tar(backend, "expires.txt", b"temporary")
        time.sleep(1.1)

        async with app.state.mcp_server.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost:16001",
            ) as client:
                sid = await _init(client)
                result = await _call(
                    client, "cassetta_get", {"path": "expires.txt"}, sid,
                )
                assert result["isError"] is True
                assert "not found" in result["content"][0]["text"].lower()


# ============================================================
# US3: cassetta_list
# ============================================================


class TestCassettaList:

    @pytest.mark.asyncio
    async def test_list_all_files(self, mcp_app: tuple) -> None:
        _app, client, backend = mcp_app
        await _put_tar(backend, "reports/q1.txt", b"Q1 report")
        await _put_tar(backend, "data/input.csv", b"a,b,c")
        sid = await _init(client)

        result = await _call(client, "cassetta_list", {}, sid)
        assert result.get("isError") is not True
        text = result["content"][0]["text"]
        assert "reports/q1.txt" in text
        assert "data/input.csv" in text

    @pytest.mark.asyncio
    async def test_list_with_prefix_filter(self, mcp_app: tuple) -> None:
        _app, client, backend = mcp_app
        await _put_tar(backend, "reports/q1.txt", b"Q1")
        await _put_tar(backend, "reports/q2.txt", b"Q2")
        await _put_tar(backend, "data/input.csv", b"csv")
        sid = await _init(client)

        result = await _call(client, "cassetta_list", {"prefix": "reports/"}, sid)
        assert result.get("isError") is not True
        text = result["content"][0]["text"]
        assert "reports/q1.txt" in text
        assert "reports/q2.txt" in text
        assert "data/input.csv" not in text

    @pytest.mark.asyncio
    async def test_list_empty_result(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app
        sid = await _init(client)

        result = await _call(client, "cassetta_list", {"prefix": "nonexistent/"}, sid)
        assert result.get("isError") is not True

    @pytest.mark.asyncio
    async def test_list_excludes_expired(
        self, mcp_storage_dir: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Brief 539: self-sufficient env (see test_get_expired_file_returns_error).
        monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
        monkeypatch.setenv("CASSETTA_STORAGE_PATH", mcp_storage_dir)
        monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "1")
        monkeypatch.setenv(
            "CASSETTA_MCP_ALLOWED_HOSTS",
            "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001",
        )

        app = create_app()
        config = app.state.config
        backends = app.state.backends
        backend = backends.backend
        configure_mcp(config, backends)

        await _put_tar(backend, "old.txt", b"old")
        time.sleep(1.1)
        await _put_tar(backend, "new.txt", b"new")

        async with app.state.mcp_server.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost:16001",
            ) as client:
                sid = await _init(client)
                result = await _call(client, "cassetta_list", {}, sid)
                text = result["content"][0]["text"]
                assert "new.txt" in text
                assert "old.txt" not in text


# ============================================================
# US4: cassetta_delete
# ============================================================


class TestCassettaDelete:

    @pytest.mark.asyncio
    async def test_delete_existing_file(self, mcp_app: tuple) -> None:
        _app, client, backend = mcp_app
        await _put_tar(backend, "temp/scratch.txt", b"delete me")
        sid = await _init(client)

        result = await _call(
            client, "cassetta_delete", {"path": "temp/scratch.txt"}, sid,
        )
        assert result.get("isError") is not True
        assert "temp/scratch.txt" in result["content"][0]["text"]
        import pytest as _pytest
        with _pytest.raises(FileNotFoundError):
            await backend.read_bundle_meta("store/temp/scratch.txt")

    @pytest.mark.asyncio
    async def test_delete_missing_file_returns_error(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app
        sid = await _init(client)

        result = await _call(
            client, "cassetta_delete", {"path": "nonexistent.txt"}, sid,
        )
        assert result["isError"] is True
        assert "not found" in result["content"][0]["text"].lower()


# ============================================================
# US6: Error Handling
# ============================================================


class TestErrorHandling:

    @pytest.mark.asyncio
    async def test_error_no_cloud_mention(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app
        sid = await _init(client)

        result = await _call(client, "cassetta_put", {
            "path": "../secret", "content": "test",
        }, sid)
        text = result["content"][0]["text"].lower()
        assert "cloud" not in text
        assert "premium" not in text
        assert "upgrade" not in text

    @pytest.mark.asyncio
    async def test_invalid_path_error(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app
        sid = await _init(client)

        result = await _call(client, "cassetta_get", {"path": "/absolute/path"}, sid)
        assert result["isError"] is True


# ============================================================
# Cross-protocol: MCP ↔ REST interop
# ============================================================


class TestCrossProtocol:

    @pytest.mark.asyncio
    async def test_put_via_mcp_get_via_rest(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app
        sid = await _init(client)

        result = await _call(client, "cassetta_put", {
            "path": "shared/data.json", "content": '{"via": "mcp"}',
        }, sid)
        assert result.get("isError") is not True

        resp = await client.get("/files/shared/data.json")
        assert resp.status_code == 200
        envelope = resp.json()
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == '{"via": "mcp"}'

    @pytest.mark.asyncio
    async def test_put_via_rest_get_via_mcp(self, mcp_app: tuple) -> None:
        _app, client, _backend = mcp_app

        resp = await client.put(
            "/files/shared/rest-data.txt", content=b"stored via REST",
        )
        assert resp.status_code == 201

        sid = await _init(client)
        result = await _call(
            client, "cassetta_get", {"path": "shared/rest-data.txt"}, sid,
        )
        assert result.get("isError") is not True
        envelope = json.loads(result["content"][0]["text"])
        assert envelope["mode"] == "inline"
        assert envelope["files"][0]["content"] == "stored via REST"
