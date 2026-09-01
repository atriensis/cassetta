"""T050 — Observability audit: FR-027 event contract for uploads.

Drives one inline round-trip and one batch round-trip, captures
structured log events through a handler attached directly to the
``cassetta`` logger (which is configured with ``propagate=False`` by
:func:`cassetta.structured_log.configure_logging`, so ``caplog`` /
propagation to root would miss these events), and asserts:

- ``upload_init`` is emitted exactly once per upload (at ``send_init``).
- ``upload_stream_start`` is emitted exactly once per upload.
- ``upload_stream_complete`` is emitted exactly once per upload.
- The ``bundle_id`` in the ``detail`` payload of all three events
  matches the ``bundle_id`` returned by ``send_init``.

T044 (Brief 515) extends the contract to the download surface (FR-026):

- ``download_mode_decision`` fires on every read surface after policy eval.
- ``download_claim_issued`` fires on inbox reference-mode pick.
- ``download_file_fetched`` fires once per successful file stream.
- ``download_claim_completed`` fires when the last file in an inbox
  bundle is fetched and the bundle + claim are both deleted.
- ``download_jwt_validation_failed`` fires on any 401-class failure.
- ``download_identity_mismatch`` fires on a 403-class identity mismatch.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import logging
import tarfile
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
import pytest


@dataclass
class _CapturedEvent:
    event: str
    detail: dict[str, Any] | None


class _EventHandler(logging.Handler):
    """Collect structured log events emitted via ``struct_log``."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[_CapturedEvent] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        event = getattr(record, "event", None) or record.getMessage()
        detail = getattr(record, "detail", None)
        self.records.append(_CapturedEvent(event=str(event), detail=detail))

    def events_for(self, name: str) -> list[_CapturedEvent]:
        return [r for r in self.records if r.event == name]


def _unwrap(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _build_tar(files: dict[str, bytes], *, compress: bool) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return gzip.compress(raw) if compress else raw


@pytest.fixture
def log_capture() -> Any:
    """Attach a capturing handler directly to ``cassetta`` AND ``cassetta.auth``.

    ``configure_logging`` sets ``propagate=False`` on both, so neither
    caplog nor any handler attached to the root logger sees these events
    — and Brief 533 FR-041 makes ``cassetta.auth`` propagation independent
    of ``cassetta``, so attaching to ``cassetta`` alone is no longer
    enough to see ``cassetta.auth.*`` records.
    """
    handler = _EventHandler()
    attached: list[tuple[logging.Logger, int]] = []
    for name in ("cassetta", "cassetta.auth"):
        log = logging.getLogger(name)
        attached.append((log, log.level))
        log.setLevel(logging.DEBUG)
        log.addHandler(handler)
    try:
        yield handler
    finally:
        for log, prior in attached:
            log.removeHandler(handler)
            log.setLevel(prior)


def _bundle_id_of(event: _CapturedEvent) -> str | None:
    if not isinstance(event.detail, dict):
        return None
    value = event.detail.get("bundle_id")
    return str(value) if value else None


def _assert_lifecycle_once(
    handler: _EventHandler, bundle_id: str, *, label: str,
) -> None:
    for name in ("upload_init", "upload_stream_start", "upload_stream_complete"):
        matches = [
            e for e in handler.events_for(name) if _bundle_id_of(e) == bundle_id
        ]
        assert len(matches) == 1, (
            f"{label}: expected exactly one {name!r} for bundle_id={bundle_id!r}, "
            f"got {len(matches)}: {[e.detail for e in handler.events_for(name)]!r}"
        )


@pytest.mark.asyncio
async def test_inline_upload_emits_fr027_events(
    core_app: tuple[httpx.AsyncClient, str], h, log_capture: _EventHandler,
) -> None:
    client, _ = core_app
    sender = await h.setup_agent(client, "bob", "obs-inline")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    content = b"hello inline\n"
    init = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "notes.md",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "notes.md", "size": len(content)}],
            },
        },
        sid=sid, api_key=sender,
    )
    body = _unwrap(init)
    assert body["mode"] == "inline"
    bundle_id = body["bundle_id"]

    send = await h.mcp_call(
        client, "cassetta_send_inline",
        {
            "token": body["inline_token"],
            "files": [{
                "name": "notes.md",
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            }],
        },
        sid=sid, api_key=sender,
    )
    send_body = _unwrap(send)
    assert send_body == {"bundle_id": bundle_id, "ok": True}

    _assert_lifecycle_once(log_capture, bundle_id, label="inline")


@pytest.mark.asyncio
async def test_batch_upload_emits_fr027_events(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
    log_capture: _EventHandler,
) -> None:
    client, _ = core_app_small_inline
    sender = await h.setup_agent(client, "bob", "obs-batch")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    files = {
        "payload.bin": b"x" * 200,
        "README.md": b"# hi\n" + b"y" * 128,
    }
    manifest = {
        "file_count": len(files),
        "files": [
            {"name": name, "size": len(data)} for name, data in files.items()
        ],
    }
    init = await h.mcp_call(
        client, "cassetta_send_init",
        {"to": "alice:main", "path": "drop.tgz", "manifest": manifest},
        sid=sid, api_key=sender,
    )
    body = _unwrap(init)
    assert body["mode"] == "batch"
    bundle_id = body["bundle_id"]

    url_path = urllib.parse.urlparse(body["upload_url"]).path
    tar_bytes = _build_tar(files, compress=False)
    resp = await client.post(
        url_path,
        content=tar_bytes,
        headers={
            "Authorization": f"Bearer {body['batch_token']}",
            "Content-Type": "application/x-tar",
        },
    )
    assert resp.status_code == 201, (resp.status_code, resp.text)
    assert resp.json() == {"bundle_id": bundle_id, "ok": True}

    _assert_lifecycle_once(log_capture, bundle_id, label="batch")


@pytest.mark.asyncio
async def test_both_modes_same_session_keep_bundle_ids_distinct(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
    log_capture: _EventHandler,
) -> None:
    """Drive one inline then one batch upload; assert each bundle_id's
    three lifecycle events are present and bundle_ids do not collide.
    """
    client, _ = core_app_small_inline
    sender = await h.setup_agent(client, "bob", "obs-both")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    # --- Inline --------------------------------------------------------------
    small = b"tiny\n"
    init_inline = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "small.txt",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "small.txt", "size": len(small)}],
            },
        },
        sid=sid, api_key=sender,
    )
    ib = _unwrap(init_inline)
    assert ib["mode"] == "inline"
    inline_bundle_id = ib["bundle_id"]
    await h.mcp_call(
        client, "cassetta_send_inline",
        {
            "token": ib["inline_token"],
            "files": [{
                "name": "small.txt",
                "content": base64.b64encode(small).decode("ascii"),
                "encoding": "base64",
            }],
        },
        sid=sid, api_key=sender,
    )

    # --- Batch ---------------------------------------------------------------
    batch_files = {"blob.bin": b"z" * 256}
    init_batch = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "big.tgz",
            "manifest": {
                "file_count": 1,
                "files": [{"name": "blob.bin", "size": len(batch_files["blob.bin"])}],
            },
        },
        sid=sid, api_key=sender,
    )
    bb = _unwrap(init_batch)
    assert bb["mode"] == "batch"
    batch_bundle_id = bb["bundle_id"]
    url_path = urllib.parse.urlparse(bb["upload_url"]).path
    tar_bytes = _build_tar(batch_files, compress=False)
    resp = await client.post(
        url_path, content=tar_bytes,
        headers={
            "Authorization": f"Bearer {bb['batch_token']}",
            "Content-Type": "application/x-tar",
        },
    )
    assert resp.status_code == 201

    assert inline_bundle_id != batch_bundle_id
    _assert_lifecycle_once(log_capture, inline_bundle_id, label="inline")
    _assert_lifecycle_once(log_capture, batch_bundle_id, label="batch")


# --- Brief 515: download-surface FR-026 events -----------------------------


@pytest.mark.asyncio
async def test_download_reference_pick_emits_fr026_events(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
    log_capture: _EventHandler,
) -> None:
    """Reference-mode pick + full fetch: every FR-026 event fires
    with its expected `detail` keys.

    Covers the happy-path quartet:
    - ``download_mode_decision``  (at pick time, `reason=over_threshold`)
    - ``download_claim_issued``   (sidecar written with this jti)
    - ``download_file_fetched``   (one per file, with `bytes_transferred`)
    - ``download_claim_completed``(once, after last file, bundle deleted)
    """
    import os
    from pathlib import Path

    from cassetta.backends.filesystem.storage import FilesystemBackend

    from .conftest import seed_inbox_bundle

    client, _ = core_app_small_inline
    storage_dir = os.environ["CASSETTA_STORAGE_PATH"]
    sender_key = await h.setup_agent(client, "bob", "main")
    alice_key = await h.create_key(client, sender_key, "alice", "main")

    backend = FilesystemBackend(root_path=storage_dir)
    files = [
        ("notes.md", b"# obs\n" + b"a" * 100),
        ("src/lib.py", b"print('obs')\n" * 20),
    ]
    await seed_inbox_bundle(
        backend, "alice:main", "obs-drop",
        files=files, sender="bob",
    )

    sid = await h.mcp_init(client, api_key=alice_key)
    pick = await h.mcp_call(
        client, "cassetta_pick", {"path": "obs-drop"},
        sid=sid, api_key=alice_key,
    )
    envelope = _unwrap(pick)
    assert envelope["mode"] == "reference", envelope
    token = envelope["download_token"]
    bundle_id = envelope["bundle"]["bundle_id"]

    # download_mode_decision — emitted by the pick MCP handler before
    # the envelope is returned. Detail keys: bundle_id, mode, namespace.
    mode_events = log_capture.events_for("download_mode_decision")
    assert mode_events, "no download_mode_decision emitted"
    mode_latest = mode_events[-1]
    assert isinstance(mode_latest.detail, dict)
    assert mode_latest.detail.get("mode") == "reference"
    assert mode_latest.detail.get("namespace") == "inbox"
    assert mode_latest.detail.get("bundle_id") == bundle_id

    # download_claim_issued — one sidecar; detail keys: bundle_id, jti,
    # files, exp, recipient, namespace.
    issued_events = log_capture.events_for("download_claim_issued")
    assert len(issued_events) == 1, issued_events
    issued = issued_events[0]
    assert isinstance(issued.detail, dict)
    assert issued.detail.get("recipient") == "alice:main"
    assert issued.detail.get("namespace") == "inbox"
    assert issued.detail.get("bundle_id") == bundle_id
    assert "jti" in issued.detail
    assert "exp" in issued.detail
    assert isinstance(issued.detail.get("files"), list)
    jti = issued.detail["jti"]

    # Fetch every file with the download token + X-Sender=alice:main.
    import urllib.parse

    headers = {"Authorization": f"Bearer {token}", "X-Sender": "alice:main"}
    for file_entry in envelope["files"]:
        url_path = urllib.parse.urlparse(file_entry["url"]).path
        resp = await client.get(url_path, headers=headers)
        assert resp.status_code == 200, resp.text

    # download_file_fetched — one per file. Detail keys: bundle_id, jti,
    # name, bytes_transferred.
    fetched_events = log_capture.events_for("download_file_fetched")
    assert len(fetched_events) == len(files), fetched_events
    for ev in fetched_events:
        assert isinstance(ev.detail, dict)
        assert ev.detail.get("bundle_id") == bundle_id
        assert ev.detail.get("jti") == jti
        assert "name" in ev.detail
        assert isinstance(ev.detail.get("bytes_transferred"), int)
        assert ev.detail["bytes_transferred"] > 0

    # download_claim_completed — emitted once. Detail keys: bundle_id,
    # jti, total_bytes, namespace.
    completed_events = log_capture.events_for("download_claim_completed")
    assert len(completed_events) == 1, completed_events
    completed = completed_events[0]
    assert isinstance(completed.detail, dict)
    assert completed.detail.get("jti") == jti
    assert completed.detail.get("bundle_id") == bundle_id
    assert completed.detail.get("namespace") == "inbox"

    # Bundle directory is gone after completion.
    assert not (Path(storage_dir) / "data" / "inbox" / "alice:main" /
                "obs-drop").exists()


@pytest.mark.asyncio
async def test_download_error_paths_emit_fr026_events(
    core_app_small_inline: tuple[httpx.AsyncClient, str],
    h,
    log_capture: _EventHandler,
) -> None:
    """401 path (bad JWT) and 403 path (identity mismatch) each emit
    the appropriate FR-026 event exactly once.
    """
    import os

    from cassetta.backends.filesystem.storage import FilesystemBackend

    from .conftest import seed_inbox_bundle

    client, _ = core_app_small_inline
    storage_dir = os.environ["CASSETTA_STORAGE_PATH"]
    sender_key = await h.setup_agent(client, "bob", "main")
    alice_key = await h.create_key(client, sender_key, "alice", "main")
    # Eve has a valid api key on a different identity, used for the 403
    # identity-mismatch path.
    await h.create_key(client, sender_key, "eve", "main")

    backend = FilesystemBackend(root_path=storage_dir)
    await seed_inbox_bundle(
        backend, "alice:main", "err-drop",
        files=[("a.bin", b"x" * 500)], sender="bob",
    )

    sid = await h.mcp_init(client, api_key=alice_key)
    pick = await h.mcp_call(
        client, "cassetta_pick", {"path": "err-drop"},
        sid=sid, api_key=alice_key,
    )
    envelope = _unwrap(pick)
    assert envelope["mode"] == "reference"
    token = envelope["download_token"]

    import urllib.parse

    url_path = urllib.parse.urlparse(envelope["files"][0]["url"]).path

    # --- 401: bad JWT signature -----------------------------------------
    resp_401 = await client.get(
        url_path,
        headers={
            "Authorization": "Bearer not-a-real-jwt.tamper.tamper",
            "X-Sender": "alice:main",
        },
    )
    assert resp_401.status_code == 401, resp_401.text
    # Brief 529 R4: download_jwt_validation_failed → auth.failure source=download.
    jwt_events = [
        e for e in log_capture.events_for("auth.failure")
        if isinstance(e.detail, dict) and e.detail.get("source") == "download"
    ]
    assert jwt_events, "no auth.failure (source=download) emitted"
    assert isinstance(jwt_events[-1].detail, dict)
    assert jwt_events[-1].detail.get("reason") in {"jwt_invalid", "jwt_expired"}

    # --- 403: JWT valid, identity mismatch ------------------------------
    # Send the same token but X-Sender = eve:main (authenticated but not
    # the recipient). claim.recipient is alice:main.
    resp_403 = await client.get(
        url_path,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Sender": "eve:main",
        },
    )
    assert resp_403.status_code == 403, resp_403.text
    mism_events = log_capture.events_for("download_identity_mismatch")
    assert mism_events, "no download_identity_mismatch emitted"
    latest = mism_events[-1]
    assert isinstance(latest.detail, dict)
    assert latest.detail.get("claim_recipient") == "alice:main"
    assert latest.detail.get("identity_label") == "eve:main"
    assert "bundle_path" in latest.detail
