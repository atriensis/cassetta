"""T049 / SC-006 — Disk-pressure probe for the streaming batch upload.

Generates a 256 MiB temp file (CI-scaled down from 1 GiB), drives it
through ``cassetta_send_init`` → ``POST /upload/{bundle_path}`` while a
sibling task samples the data-root's used-bytes every 100 ms, and
asserts the upload never holds more than ``file_size + 32 MiB`` of
extra disk at any instant.

The constitution permits a 32 MiB slack for OS buffering and the
``meta.json`` sidecar. A regression that buffers the full body into
memory or into a temp copy would spike the delta by N× file_size, so
this probe catches that cheaply.

Gated behind ``@pytest.mark.slow`` so the normal suite stays fast;
opt-in via ``pytest -m slow``. Skipped when the data-root disk has
less than 1 GiB free.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import shutil
import tarfile
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

# Scale the full 1 GiB probe down for CI; the Pi smoke runs the full variant
# out-of-band (documented in the task list).
FILE_SIZE_BYTES = 256 * 1024 * 1024  # 256 MiB
SLACK_BYTES = 32 * 1024 * 1024       # 32 MiB buffering slack
REQUIRED_FREE_BYTES = 1024 * 1024 * 1024  # need >=1 GiB free to run


def _make_payload(path: Path, size: int) -> None:
    """Write a ``size``-byte file with pseudo-random content.

    Pseudo-random so gzip (if the caller chose compression) cannot
    collapse it, which would defeat the point of the probe.
    """
    chunk = os.urandom(1024 * 1024)  # 1 MiB repeating block
    remaining = size
    with path.open("wb") as fh:
        while remaining > 0:
            take = min(len(chunk), remaining)
            fh.write(chunk[:take])
            remaining -= take


def _build_tar_to_file(src: Path, arcname: str, dst: Path) -> None:
    """Build an uncompressed tar containing one entry at ``arcname``."""
    with tarfile.open(dst, mode="w") as tf:  # no gzip — stresses streaming path
        tf.add(src, arcname=arcname)


async def _stream_file(path: Path, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """Yield the file contents as async byte chunks.

    Using a real async generator (not a ``bytes`` buffer) so
    ``httpx.AsyncClient`` reads off disk as it sends — otherwise the
    body is resident in memory and the whole point of the probe is
    lost. Yielding between reads also lets the sampler task run.
    """
    with path.open("rb") as fh:
        while True:
            data = fh.read(chunk_size)
            if not data:
                return
            yield data
            # Cooperative yield so the disk-usage sampler gets a slice.
            await asyncio.sleep(0)


@pytest.fixture
def _allow_large_per_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Brief 531: 100 MiB default would reject this 256 MiB probe."""
    monkeypatch.setenv("CASSETTA_PER_FILE_MAX", str(512 * 1024 * 1024))


@pytest.mark.slow
@pytest.mark.asyncio
async def test_batch_upload_disk_pressure_bounded(
    _allow_large_per_file: None,
    core_app_small_inline: tuple[httpx.AsyncClient, str], h, tmp_path: Path,
) -> None:
    client, _ = core_app_small_inline
    app = client._transport.app  # type: ignore[attr-defined]
    storage_path = app.state.config.storage_path
    data_root = os.path.join(storage_path, "data")
    os.makedirs(data_root, exist_ok=True)

    free_bytes = shutil.disk_usage(data_root).free
    if free_bytes < REQUIRED_FREE_BYTES:
        pytest.skip(
            f"disk has {free_bytes} bytes free; need >={REQUIRED_FREE_BYTES}",
        )

    # Prepare the payload and the matching tar on disk (not in memory — we
    # want the upload body to stream from a file handle).
    payload_name = "big.bin"
    payload_path = tmp_path / payload_name
    _make_payload(payload_path, FILE_SIZE_BYTES)
    tar_path = tmp_path / "payload.tar"
    _build_tar_to_file(payload_path, payload_name, tar_path)
    assert tar_path.stat().st_size >= FILE_SIZE_BYTES  # tar adds headers/padding

    # Identities + init.
    sender = await h.setup_agent(client, "bob", "disk-pressure")
    await h.create_key(client, sender, "alice", "main")
    sid = await h.mcp_init(client, api_key=sender)

    init_result = await h.mcp_call(
        client, "cassetta_send_init",
        {
            "to": "alice:main", "path": "big.tgz",
            "manifest": {
                "file_count": 1,
                "files": [{"name": payload_name, "size": FILE_SIZE_BYTES}],
            },
        },
        sid=sid, api_key=sender,
    )
    init_body = _json.loads(init_result["content"][0]["text"])
    assert init_body["mode"] == "batch", init_body
    upload_url = init_body["upload_url"]
    token = init_body["batch_token"]
    bundle_id = init_body["bundle_id"]
    url_path = urllib.parse.urlparse(upload_url).path

    initial_used = shutil.disk_usage(data_root).used
    samples: list[int] = [initial_used]
    stop = asyncio.Event()

    async def _sample() -> None:
        while not stop.is_set():
            samples.append(shutil.disk_usage(data_root).used)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    sampler = asyncio.create_task(_sample())
    try:
        resp = await client.post(
            url_path,
            content=_stream_file(tar_path),  # async generator
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-tar",
            },
            timeout=None,
        )
    finally:
        stop.set()
        await sampler

    assert resp.status_code == 201, (resp.status_code, resp.text)
    assert resp.json() == {"bundle_id": bundle_id, "ok": True}

    # Final sample after commit.
    samples.append(shutil.disk_usage(data_root).used)
    peak_delta = max(samples) - initial_used
    allowed = FILE_SIZE_BYTES + SLACK_BYTES
    assert peak_delta <= allowed, (
        f"peak disk delta during upload was {peak_delta} bytes "
        f"(file_size={FILE_SIZE_BYTES}, slack={SLACK_BYTES}, "
        f"allowed={allowed}); implies unbounded buffering"
    )

    # Sanity: the committed bundle is visible on disk under the expected path.
    bundle_dir = os.path.join(data_root, "inbox", "alice:main", "big.tgz")
    assert os.path.isdir(bundle_dir)
    assert os.path.isfile(os.path.join(bundle_dir, "meta.json"))

    # Drop the source tar + payload to free the tmp space before
    # pytest teardown (we have our own copy in ``data_root`` now).
    try:
        tar_path.unlink()
        payload_path.unlink()
    except FileNotFoundError:
        pass
