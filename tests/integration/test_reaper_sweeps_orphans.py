"""Passive reaper sweeps orphan bundle directories.

Creates orphan directories on disk (bypassing the API) and runs
``gc.sweep`` directly, asserting age-based retention.
"""

from __future__ import annotations

import os
import time

import pytest

from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.gc import sweep


@pytest.mark.asyncio
async def test_old_orphan_is_reaped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = str(tmp_path)
    backend = FilesystemBackend(root_path=root)
    orphan_dir = os.path.join(root, "data", "inbox", "alice", "old.bin")
    os.makedirs(orphan_dir, exist_ok=True)
    with open(os.path.join(orphan_dir, "content.bin"), "wb") as f:
        f.write(b"x" * 16)

    # Backdate mtime on the bundle dir to ensure "old" classification.
    ago = time.time() - 7200
    os.utime(orphan_dir, (ago, ago))

    await sweep(backend, min_age_s=3600)
    assert not os.path.exists(orphan_dir)


@pytest.mark.asyncio
async def test_young_orphan_is_preserved(tmp_path) -> None:  # type: ignore[no-untyped-def]
    root = str(tmp_path)
    backend = FilesystemBackend(root_path=root)
    orphan_dir = os.path.join(root, "data", "inbox", "alice", "fresh.bin")
    os.makedirs(orphan_dir, exist_ok=True)
    with open(os.path.join(orphan_dir, "content.bin"), "wb") as f:
        f.write(b"x" * 16)
    # mtime is ~now by default; don't age it.

    await sweep(backend, min_age_s=3600)
    assert os.path.exists(orphan_dir)
