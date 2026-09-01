"""T029 (US3) — inline-mode ``cassetta_pick`` returns the unified envelope.

Single-file and multi-file bundles both return:
``{"mode": "inline", "bundle": {...}, "files": [{"name", "content", "encoding"}, ...]}``.

The Brief 514 raw-UTF-8-string return for single-file pick/get is
retired — ``json.loads(response)`` MUST succeed and the result has a
``"mode": "inline"`` key.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx
import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend

from .conftest import seed_inbox_bundle


def _unwrap_text(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.mark.asyncio
async def test_single_file_inline_returns_envelope(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    """Single-file bundle → unified envelope, NOT a bare string."""
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "main")
    alice_key = await h.create_key(client, sender_key, "alice", "main")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    await seed_inbox_bundle(
        backend,
        "alice:main",
        "note.md",
        content=b"# Hello\nmeeting notes\n",
        sender="bob",
    )

    sid = await h.mcp_init(client, api_key=alice_key)
    result = await h.mcp_call(
        client,
        "cassetta_pick",
        {"path": "note.md"},
        sid=sid,
        api_key=alice_key,
    )
    raw = _unwrap_text(result)
    # Parseable as JSON envelope — NOT a bare string.
    envelope = json.loads(raw)
    assert isinstance(envelope, dict)
    assert envelope["mode"] == "inline"
    assert "bundle" in envelope
    assert len(envelope["files"]) == 1
    entry = envelope["files"][0]
    assert entry["name"] == "note.md"
    assert entry["content"] == "# Hello\nmeeting notes\n"
    assert entry.get("encoding", "utf8") == "utf8"


@pytest.mark.asyncio
async def test_multi_file_inline_returns_envelope(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "main")
    alice_key = await h.create_key(client, sender_key, "alice", "main")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    files = [
        ("foo.md", b"# foo\n"),
        ("bar.txt", b"hello\n"),
    ]
    await seed_inbox_bundle(
        backend,
        "alice:main",
        "pair",
        files=files,
        sender="bob",
    )

    sid = await h.mcp_init(client, api_key=alice_key)
    result = await h.mcp_call(
        client,
        "cassetta_pick",
        {"path": "pair"},
        sid=sid,
        api_key=alice_key,
    )
    envelope = json.loads(_unwrap_text(result))
    assert envelope["mode"] == "inline"
    assert {e["name"] for e in envelope["files"]} == {"foo.md", "bar.txt"}
    by_name = {e["name"]: e for e in envelope["files"]}
    assert by_name["foo.md"]["content"] == "# foo\n"
    assert by_name["bar.txt"]["content"] == "hello\n"


@pytest.mark.asyncio
async def test_non_utf8_inline_returns_base64(
    core_app: tuple[httpx.AsyncClient, str],
    h,
) -> None:
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "main")
    alice_key = await h.create_key(client, sender_key, "alice", "main")

    storage_root = Path(os.environ["CASSETTA_STORAGE_PATH"])
    backend = FilesystemBackend(root_path=str(storage_root))
    # A 1-byte PNG-ish binary: invalid UTF-8.
    payload = b"\x89PNG\r\n\x1a\n\xff\xfe"
    await seed_inbox_bundle(
        backend,
        "alice:main",
        "image.png",
        content=payload,
        sender="bob",
    )

    sid = await h.mcp_init(client, api_key=alice_key)
    result = await h.mcp_call(
        client,
        "cassetta_pick",
        {"path": "image.png"},
        sid=sid,
        api_key=alice_key,
    )
    envelope = json.loads(_unwrap_text(result))
    assert envelope["mode"] == "inline"
    entry = envelope["files"][0]
    assert entry["encoding"] == "base64"
    assert base64.b64decode(entry["content"]) == payload
