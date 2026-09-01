"""T013 — failing-first tests for ``cassetta.gc`` passive reaper.

A fake ``StorageBackend`` narrowly implements ``list_bundles`` and
``delete_bundle`` so the tests stay Layer-2 pure.

Covered:
- Orphans older than ``min_age_s`` are deleted.
- Orphans younger than ``min_age_s`` are preserved.
- Committed bundles (``has_meta=True``) are skipped regardless of age.
- A structured log event ``gc_reaped`` is emitted per deletion.
- Sweep is idempotent: a second run back-to-back is a no-op.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import pytest

from cassetta.gc import sweep
from cassetta.protocols.storage import BundleRef


@dataclass
class FakeBackend:
    """Minimal fake covering the methods ``gc.sweep`` exercises."""

    refs: list[BundleRef] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def list_bundles(
        self, prefix: str, *, include_orphans: bool = False,
    ) -> Iterable[BundleRef]:
        # include_orphans must be True for the reaper's purpose.
        assert include_orphans is True
        return [r for r in self.refs if r.path.startswith(prefix)]

    async def delete_bundle(self, path: str) -> None:
        self.refs = [r for r in self.refs if r.path != path]
        self.deleted.append(path)


@pytest.mark.asyncio
async def test_old_orphans_deleted() -> None:
    now = time.time()
    backend = FakeBackend(refs=[
        BundleRef(path="inbox/alice/stale.bin", has_meta=False, mtime=now - 7200),
    ])
    await sweep(backend, min_age_s=3600)
    assert backend.deleted == ["inbox/alice/stale.bin"]


@pytest.mark.asyncio
async def test_young_orphans_preserved() -> None:
    now = time.time()
    backend = FakeBackend(refs=[
        BundleRef(path="inbox/alice/fresh.bin", has_meta=False, mtime=now - 60),
    ])
    await sweep(backend, min_age_s=3600)
    assert backend.deleted == []


@pytest.mark.asyncio
async def test_committed_bundles_skipped_regardless_of_age() -> None:
    now = time.time()
    backend = FakeBackend(refs=[
        BundleRef(path="inbox/alice/old-ok.txt", has_meta=True, mtime=now - 999_999),
        BundleRef(path="store/doc.txt",         has_meta=True, mtime=now - 999_999),
    ])
    await sweep(backend, min_age_s=3600)
    assert backend.deleted == []


@pytest.mark.asyncio
async def test_reaper_emits_structured_log() -> None:
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.INFO)
    cassetta_logger = logging.getLogger("cassetta")
    # Brief 539: pin the logger level so the INFO ``gc_reaped`` record is
    # emitted regardless of ordering (in isolation the level defaults to
    # WARNING and would filter it); restore at teardown to avoid leaking.
    prior_level = cassetta_logger.level
    cassetta_logger.addHandler(handler)
    cassetta_logger.setLevel(logging.INFO)
    try:
        now = time.time()
        backend = FakeBackend(refs=[
            BundleRef(path="inbox/bob/junk", has_meta=False, mtime=now - 7200),
        ])
        await sweep(backend, min_age_s=3600)
    finally:
        cassetta_logger.removeHandler(handler)
        cassetta_logger.setLevel(prior_level)

    events = [r for r in captured if getattr(r, "event", "") == "gc_reaped"]
    assert len(events) == 1
    detail = events[0].detail  # type: ignore[attr-defined]
    assert detail["path"] == "inbox/bob/junk"
    assert detail["age_seconds"] >= 7200


@pytest.mark.asyncio
async def test_sweep_is_idempotent_back_to_back() -> None:
    now = time.time()
    backend = FakeBackend(refs=[
        BundleRef(path="inbox/alice/stale.bin", has_meta=False, mtime=now - 7200),
    ])
    await sweep(backend, min_age_s=3600)
    assert backend.deleted == ["inbox/alice/stale.bin"]
    # Second sweep finds nothing to delete.
    await sweep(backend, min_age_s=3600)
    assert backend.deleted == ["inbox/alice/stale.bin"]
