"""Full `cassetta send` round-trip against an in-process ASGI app.

Drives the real ``send`` code path (``cassetta.cli.send._send_async``) end to end:
phase 1 (``POST /uploads`` → batch token + upload_url) then phase 2 (stream a tar to
``upload_url``), exactly as ``test_send_init_batch.py`` drives the raw HTTP path — but
through the command's own core, with the fixture's ``httpx.AsyncClient`` injected.

After the send, the recipient retrieves the bundle via ``cassetta_pick`` and the delivered
bytes are asserted byte-identical to the originals. ``_pick_content`` handles both the inline
and reference pick envelopes (a batch bundle picks inline or reference depending on stored
size vs ``max_inline_size``).
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from cassetta.cli.download import _decode_recipient
from cassetta.cli.send import _send_async

# Deterministic ~1 MB payload, under the fixture's CASSETTA_MAX_FILE_SIZE (1_048_576).
_LARGE = (b"0123456789abcdef" * (1_000_000 // 16 + 1))[:1_000_000]


async def _pick_content(
    client: httpx.AsyncClient,
    h,
    api_key: str,
    sid: str,
    path: str,
) -> dict[str, bytes]:
    """Return ``{name: bytes}`` for a picked bundle, inline or reference mode."""
    pick = await h.mcp_call(
        client,
        "cassetta_pick",
        {"path": path},
        sid=sid,
        api_key=api_key,
    )
    envelope = json.loads(pick["content"][0]["text"])
    out: dict[str, bytes] = {}
    if envelope["mode"] == "inline":
        for entry in envelope["files"]:
            encoding = entry.get("encoding", "utf8")
            if encoding == "base64":
                out[entry["name"]] = base64.b64decode(entry["content"])
            else:
                out[entry["name"]] = entry["content"].encode("utf-8")
        return out
    if envelope["mode"] == "reference":
        token = envelope["download_token"]
        headers = {"Authorization": f"Bearer {token}"}
        recipient = _decode_recipient(token)
        if recipient:
            headers["X-Sender"] = recipient
        for entry in envelope["files"]:
            resp = await client.get(entry["url"], headers=headers)
            resp.raise_for_status()
            out[entry["name"]] = resp.content
        return out
    raise AssertionError(f"unexpected pick mode: {envelope['mode']!r}")


@pytest.mark.asyncio
async def test_send_batch_roundtrip(
    core_app: tuple[httpx.AsyncClient, str],
    h,
    tmp_path,
    monkeypatch,
) -> None:
    """Small multi-file send: both phases through `send`; recipient gets identical bytes."""
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "send-batch")
    recipient_key = await h.create_key(client, sender_key, "alice", "main")

    # Real files on disk at relative paths (tar.add + os.path.getsize read from cwd).
    monkeypatch.chdir(tmp_path)
    files = {
        "src/main.py": b"print('hello cassetta send')\n" * 3,
        "README.md": b"# send\n" + b"y" * 128,
    }
    for name, data in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    bundle_id = await _send_async(
        client,
        to="alice:main",
        path="drop.tgz",
        files=list(files),
        api_key=sender_key,
    )
    assert bundle_id, "send did not return a bundle_id"

    recip_sid = await h.mcp_init(client, api_key=recipient_key)
    inbox = await h.mcp_call(
        client,
        "cassetta_inbox",
        {"agent": "alice:main"},
        sid=recip_sid,
        api_key=recipient_key,
    )
    listing = json.loads(inbox["content"][0]["text"])
    assert "drop.tgz" in {entry["path"] for entry in listing}

    got = await _pick_content(client, h, recipient_key, recip_sid, "drop.tgz")
    assert got == files


@pytest.mark.asyncio
async def test_send_large_payload_roundtrip(
    core_app: tuple[httpx.AsyncClient, str],
    h,
    tmp_path,
    monkeypatch,
) -> None:
    """~1 MB file completes both phases through `send`; retrieved bytes are identical."""
    client, _ = core_app
    sender_key = await h.setup_agent(client, "bob", "send-large")
    recipient_key = await h.create_key(client, sender_key, "alice", "main")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "payload.bin").write_bytes(_LARGE)

    bundle_id = await _send_async(
        client,
        to="alice:main",
        path="big.tgz",
        files=["payload.bin"],
        api_key=sender_key,
    )
    assert bundle_id

    recip_sid = await h.mcp_init(client, api_key=recipient_key)
    got = await _pick_content(client, h, recipient_key, recip_sid, "big.tgz")
    assert got == {"payload.bin": _LARGE}
