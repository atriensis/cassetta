"""T024 (US2) — unit tests for ``cassetta.gc.sweep_claims`` branches.

Parametrized via ``claim_storage_factory`` so a downstream distribution
can plug in its own axis. Core runs filesystem.

Covers every row in ``contracts/claim-sidecar.md`` "Reaper contract":
- active claim (age < ttl) → skipped
- expired + complete + inbox → bundle deleted + claim deleted
- expired + incomplete → claim deleted only (bundle untouched)
- expired + bundle missing → claim deleted
- malformed created_at → claim deleted defensively
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cassetta.claims import ClaimRecord
from cassetta.gc import sweep_claims


class _FakeBackend:
    """In-memory storage stub for the reaper."""

    def __init__(self) -> None:
        self.bundles: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    def list_bundles(self, prefix: str, *, include_orphans: bool = False):  # noqa: ANN001, ARG002
        return []

    async def delete_bundle(self, path: str) -> None:
        if path not in self.bundles:
            raise FileNotFoundError(path)
        del self.bundles[path]
        self.deleted.append(path)

    async def read_bundle_meta(self, path: str) -> dict[str, Any]:
        if path not in self.bundles:
            raise FileNotFoundError(path)
        return self.bundles[path]


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


def _seed_meta(names: list[str], size: int = 100) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "bundle_id": "bid",
        "sender": "sender",
        "created_at": _iso(time.time()),
        "content_type": "bundle" if len(names) > 1 else "file",
        "file_count": len(names),
        "files": [{"name": n, "size": size, "mime": "application/octet-stream"} for n in names],
    }


@pytest.fixture
def ttl_s() -> int:
    return 60


@pytest.mark.asyncio
async def test_sweep_skips_active_claim(
    claim_storage_factory,
    ttl_s: int,
) -> None:
    store = await claim_storage_factory()
    now = time.time()
    claim = ClaimRecord(
        schema_version=1,
        jti="active",
        bundle_path="inbox/a/b",
        bundle_id="bid",
        recipient="a",
        created_at=_iso(now - 10),
        files_fetched=[],
    )
    await store.issue(claim)
    backend = _FakeBackend()
    backend.bundles["inbox/a/b"] = _seed_meta(["f.bin"])

    await sweep_claims(store, backend, ttl_s=ttl_s)

    assert await store.get("active") is not None
    assert "inbox/a/b" in backend.bundles
    assert backend.deleted == []


@pytest.mark.asyncio
async def test_sweep_completes_expired_complete_inbox(
    claim_storage_factory,
    ttl_s: int,
) -> None:
    store = await claim_storage_factory()
    now = time.time()
    claim = ClaimRecord(
        schema_version=1,
        jti="done",
        bundle_path="inbox/a/c",
        bundle_id="bid",
        recipient="a",
        created_at=_iso(now - 600),
        files_fetched=["f.bin", "g.bin"],
    )
    await store.issue(claim)
    backend = _FakeBackend()
    backend.bundles["inbox/a/c"] = _seed_meta(["f.bin", "g.bin"])

    await sweep_claims(store, backend, ttl_s=ttl_s)

    assert await store.get("done") is None
    assert backend.deleted == ["inbox/a/c"]


@pytest.mark.asyncio
async def test_sweep_drops_expired_incomplete_claim(
    claim_storage_factory,
    ttl_s: int,
) -> None:
    store = await claim_storage_factory()
    now = time.time()
    claim = ClaimRecord(
        schema_version=1,
        jti="partial",
        bundle_path="inbox/a/d",
        bundle_id="bid",
        recipient="a",
        created_at=_iso(now - 600),
        files_fetched=["f.bin"],
    )
    await store.issue(claim)
    backend = _FakeBackend()
    backend.bundles["inbox/a/d"] = _seed_meta(["f.bin", "g.bin"])

    await sweep_claims(store, backend, ttl_s=ttl_s)

    assert await store.get("partial") is None
    assert "inbox/a/d" in backend.bundles
    assert backend.deleted == []


@pytest.mark.asyncio
async def test_sweep_drops_when_bundle_missing(
    claim_storage_factory,
    ttl_s: int,
) -> None:
    store = await claim_storage_factory()
    now = time.time()
    claim = ClaimRecord(
        schema_version=1,
        jti="ghost",
        bundle_path="inbox/a/e",
        bundle_id="bid",
        recipient="a",
        created_at=_iso(now - 600),
        files_fetched=[],
    )
    await store.issue(claim)
    backend = _FakeBackend()

    await sweep_claims(store, backend, ttl_s=ttl_s)

    assert await store.get("ghost") is None
    assert backend.deleted == []


@pytest.mark.asyncio
async def test_sweep_drops_malformed_created_at(
    claim_storage_factory,
    tmp_path: Path,
    ttl_s: int,
) -> None:
    """Filesystem-specific: rewrites the sidecar on disk to inject a
    bad ``created_at``. The cloud axis skips (the factory's base_dir
    isn't a POSIX path)."""
    from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage

    store = await claim_storage_factory(tmp_path)
    if not isinstance(store, FilesystemClaimStorage):
        pytest.skip("filesystem-only malformed body injection")

    claim = ClaimRecord(
        schema_version=1,
        jti="malformed",
        bundle_path="inbox/a/f",
        bundle_id="bid",
        recipient="a",
        created_at="2026-04-20T14:55:00+00:00",
        files_fetched=[],
    )
    await store.issue(claim)
    sidecar = tmp_path / "malformed.json"
    body = json.loads(sidecar.read_text())
    body["created_at"] = "not-a-date"
    sidecar.write_text(json.dumps(body))

    backend = _FakeBackend()
    await sweep_claims(store, backend, ttl_s=ttl_s)

    assert await store.get("malformed") is None
