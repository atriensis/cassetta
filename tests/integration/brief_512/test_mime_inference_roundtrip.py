"""Brief 512 US4 — end-to-end MIME inference plumbing."""

import base64
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend


@pytest.fixture
def backend(storage_dir: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_dir)


@pytest.fixture
async def mime_client(
    client: httpx.AsyncClient,
) -> AsyncIterator[httpx.AsyncClient]:
    await client.post("/keys", json={"host": "dev", "project": "agent"})
    yield client


MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


async def _mcp_init(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/mcp/",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mime-test", "version": "1.0.0"},
            },
        },
        headers=MCP_HEADERS,
    )
    return resp.headers.get("mcp-session-id", "")


async def _mcp_call(
    client: httpx.AsyncClient, name: str, args: dict, sid: str,
) -> dict:
    headers = dict(MCP_HEADERS)
    headers["mcp-session-id"] = sid
    body = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    resp = await client.post("/mcp/", json=body, headers=headers)
    return resp.json()["result"]


async def _send_inline(
    client: httpx.AsyncClient, sid: str, to: str, path: str,
    files: list[tuple[str, bytes]],
) -> None:
    init = await _mcp_call(
        client, "cassetta_send_init",
        {
            "to": to, "path": path,
            "manifest": {
                "file_count": len(files),
                "files": [{"name": n, "size": len(d)} for n, d in files],
            },
        }, sid,
    )
    body = json.loads(init["content"][0]["text"])
    assert body["mode"] == "inline"
    result = await _mcp_call(
        client, "cassetta_send_inline",
        {
            "token": body["inline_token"],
            "files": [
                {
                    "name": n,
                    "content": base64.b64encode(d).decode("ascii"),
                    "encoding": "base64",
                }
                for n, d in files
            ],
        }, sid,
    )
    assert json.loads(result["content"][0]["text"])["ok"] is True


class TestMimeInferenceRoundtrip:
    async def test_inbox_send_infers_mime(
        self, mime_client: httpx.AsyncClient, backend: FilesystemBackend,
    ) -> None:
        """Brief 514 inline send records server-inferred MIME on each file."""
        sid = await _mcp_init(mime_client)
        await _send_inline(
            mime_client, sid, to="dev:agent", path="handoff",
            files=[("plan.md", b"# Plan"), ("photo.png", b"PNG-bytes")],
        )
        meta = await backend.read_bundle_meta("inbox/dev:agent/handoff")
        mimes = {f["name"]: f["mime"] for f in meta["files"]}
        assert mimes["plan.md"] == "text/markdown"
        assert mimes["photo.png"] == "image/png"

    async def test_inbox_single_file_infers_mime(
        self, mime_client: httpx.AsyncClient, backend: FilesystemBackend,
    ) -> None:
        """Single-file inline send records the filename-inferred MIME."""
        sid = await _mcp_init(mime_client)
        await _send_inline(
            mime_client, sid, to="dev:agent", path="notes.md",
            files=[("notes.md", b"# hello")],
        )
        meta = await backend.read_bundle_meta("inbox/dev:agent/notes.md")
        assert meta["files"][0]["mime"] == "text/markdown"

    async def test_store_single_file_infers_mime(
        self, mime_client: httpx.AsyncClient, backend: FilesystemBackend,
    ) -> None:
        await mime_client.put(
            "/files/notes.md", content=b"# hello"
        )
        meta = await backend.read_bundle_meta("store/notes.md")
        assert meta["files"][0]["mime"] == "text/markdown"


class TestMimeInStoreListing:
    async def test_store_listing_exposes_mime(
        self, mime_client: httpx.AsyncClient,
    ) -> None:
        await mime_client.put("/files/doc.md", content=b"# doc")
        await mime_client.put("/files/image.png", content=b"PNG")

        listing = (await mime_client.get("/files/")).json()["files"]
        by_path = {item["path"]: item for item in listing}
        assert by_path["doc.md"]["files"][0]["mime"] == "text/markdown"
        assert by_path["image.png"]["files"][0]["mime"] == "image/png"
