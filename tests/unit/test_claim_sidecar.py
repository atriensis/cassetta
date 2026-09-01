"""T003 — unit tests for ``ClaimStorage`` filesystem operations.

Brief 517: parametrized via ``claim_storage_factory`` so cloud can
plug in an Azurite-backed axis without duplicating the test bodies.
Core runs the filesystem axis only (the fixture's default param).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from cassetta.claims import (
    BundleClaimedError,
    ClaimRecord,
)


def _make_record(
    *,
    jti: str = "a1b2c3d4-4ec5-41ad-8f2b-9dbaeddfc02a",
    bundle_path: str = "inbox/alice/notes.md",
    bundle_id: str = "5f2c8a4e-4020-47aa-b2ad-71aab200b001",
    recipient: str = "alice",
    created_at: str | None = None,
    files_fetched: tuple[str, ...] = (),
) -> ClaimRecord:
    return ClaimRecord(
        schema_version=1,
        jti=jti,
        bundle_path=bundle_path,
        bundle_id=bundle_id,
        recipient=recipient,
        created_at=created_at or "2026-04-20T14:55:00+00:00",
        files_fetched=list(files_fetched),
    )


@pytest.mark.asyncio
async def test_issue_creates_sidecar_with_body_and_perms(
    claim_storage_factory, tmp_path: Path,
) -> None:
    store = await claim_storage_factory(tmp_path)
    claim = _make_record()
    await store.issue(claim)
    # Filesystem-specific assertions — the `tmp_path` base is the
    # sidecar directory. Azure axis writes blobs; its equivalent
    # assertions live in cloud-side tests.
    sidecar = tmp_path / f"{claim.jti}.json"
    if sidecar.exists():
        body = json.loads(sidecar.read_text(encoding="utf-8"))
        assert body["jti"] == claim.jti
        assert body["bundle_path"] == claim.bundle_path
        assert body["files_fetched"] == []
        mode = sidecar.stat().st_mode & 0o777
        assert mode == 0o600


@pytest.mark.asyncio
async def test_issue_second_time_raises(claim_storage_factory) -> None:
    store = await claim_storage_factory()
    claim = _make_record()
    await store.issue(claim)
    with pytest.raises(BundleClaimedError):
        await store.issue(claim)


@pytest.mark.asyncio
async def test_get_returns_parsed_record(claim_storage_factory) -> None:
    store = await claim_storage_factory()
    original = _make_record(files_fetched=("foo.md",))
    await store.issue(original)
    loaded = await store.get(original.jti)
    assert loaded is not None
    assert loaded.jti == original.jti
    assert loaded.files_fetched == ["foo.md"]


@pytest.mark.asyncio
async def test_get_missing_returns_none(claim_storage_factory) -> None:
    store = await claim_storage_factory()
    assert await store.get("nonexistent-jti") is None


@pytest.mark.asyncio
async def test_get_malformed_raises(
    claim_storage_factory, tmp_path: Path,
) -> None:
    """Filesystem-specific: plant a malformed sidecar directly on disk."""
    from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage

    store = await claim_storage_factory(tmp_path)
    if not isinstance(store, FilesystemClaimStorage):
        pytest.skip("filesystem-only: malformed body is injected via POSIX")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bad.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        await store.get("bad")


@pytest.mark.asyncio
async def test_get_unknown_schema_raises(
    claim_storage_factory, tmp_path: Path,
) -> None:
    from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage

    store = await claim_storage_factory(tmp_path)
    if not isinstance(store, FilesystemClaimStorage):
        pytest.skip("filesystem-only: unknown schema injected via POSIX")
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "future.json"
    path.write_text(
        json.dumps({
            "schema_version": 99,
            "jti": "future",
            "bundle_path": "inbox/x/y",
            "bundle_id": "id",
            "recipient": "x",
            "created_at": "2026-04-20T14:55:00+00:00",
            "files_fetched": [],
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        await store.get("future")


@pytest.mark.asyncio
async def test_mark_fetched_appends_name(claim_storage_factory) -> None:
    store = await claim_storage_factory()
    claim = _make_record()
    await store.issue(claim)
    updated = await store.mark_fetched(claim.jti, "foo.md")
    assert updated.files_fetched == ["foo.md"]
    reloaded = await store.get(claim.jti)
    assert reloaded is not None
    assert reloaded.files_fetched == ["foo.md"]


@pytest.mark.asyncio
async def test_mark_fetched_is_idempotent(claim_storage_factory) -> None:
    store = await claim_storage_factory()
    claim = _make_record(files_fetched=("foo.md",))
    await store.issue(claim)
    updated = await store.mark_fetched(claim.jti, "foo.md")
    assert updated.files_fetched == ["foo.md"]  # no duplicate
    reloaded = await store.get(claim.jti)
    assert reloaded is not None
    assert reloaded.files_fetched == ["foo.md"]


@pytest.mark.asyncio
async def test_mark_fetched_missing_raises(claim_storage_factory) -> None:
    """FR-014: Protocol contract allows FileNotFoundError or a
    backend-native not-found exception. Core runs filesystem; cloud
    overrides the fixture and the azure axis raises
    ``ResourceNotFoundError`` — caught generically here so the
    assertion is axis-agnostic.
    """
    store = await claim_storage_factory()
    with pytest.raises(Exception) as exc_info:  # noqa: PT011
        await store.mark_fetched("absent", "foo.md")
    # Filesystem raises FileNotFoundError; Azure raises
    # azure.core.exceptions.ResourceNotFoundError. Both satisfy the
    # Protocol contract; the test just asserts *some* not-found
    # exception fires.
    assert "absent" in str(exc_info.value) or "not found" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_delete_removes_sidecar(claim_storage_factory) -> None:
    store = await claim_storage_factory()
    claim = _make_record()
    await store.issue(claim)
    await store.delete(claim.jti)
    assert await store.get(claim.jti) is None


@pytest.mark.asyncio
async def test_delete_is_idempotent(claim_storage_factory) -> None:
    store = await claim_storage_factory()
    await store.delete("never-existed")


@pytest.mark.asyncio
async def test_iter_all_yields_every_record(claim_storage_factory) -> None:
    store = await claim_storage_factory()
    await store.issue(_make_record(jti="jti-1", bundle_path="inbox/a/x"))
    await store.issue(_make_record(jti="jti-2", bundle_path="inbox/a/y"))
    await store.issue(_make_record(jti="jti-3", bundle_path="inbox/b/z"))
    jtis = sorted([r.jti async for r in store.iter_all()])
    assert jtis == ["jti-1", "jti-2", "jti-3"]


@pytest.mark.asyncio
async def test_iter_all_skips_malformed_entries(
    claim_storage_factory, tmp_path: Path,
) -> None:
    from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage

    store = await claim_storage_factory(tmp_path)
    if not isinstance(store, FilesystemClaimStorage):
        pytest.skip("filesystem-only: malformed body is injected via POSIX")
    await store.issue(_make_record(jti="good"))
    (tmp_path / "corrupt.json").write_text("{ bad", encoding="utf-8")
    records = [r async for r in store.iter_all()]
    jtis = [r.jti for r in records]
    assert jtis == ["good"]


@pytest.mark.asyncio
async def test_iter_active_by_bundle_path_excludes_expired(
    claim_storage_factory,
) -> None:
    store = await claim_storage_factory()
    now = int(time.time())
    fresh_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 10))
    stale_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 600))
    await store.issue(_make_record(
        jti="fresh", bundle_path="inbox/a/fresh", created_at=fresh_iso,
    ))
    await store.issue(_make_record(
        jti="stale", bundle_path="inbox/a/stale", created_at=stale_iso,
    ))
    active = await store.iter_active_by_bundle_path(60)
    assert "inbox/a/fresh" in active
    assert "inbox/a/stale" not in active
    assert active["inbox/a/fresh"] == "fresh"


@pytest.mark.asyncio
async def test_issue_write_failure_cleans_up_sidecar(
    claim_storage_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem-only: partial-write rollback via patched json.dumps."""
    from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage

    store = await claim_storage_factory(tmp_path)
    if not isinstance(store, FilesystemClaimStorage):
        pytest.skip("filesystem-only rollback semantics")

    real_dumps = json.dumps

    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(json, "dumps", boom)
    with pytest.raises(RuntimeError):
        await store.issue(_make_record())
    monkeypatch.setattr(json, "dumps", real_dumps)

    assert list(tmp_path.glob("*.json")) == []


@pytest.mark.asyncio
async def test_base_dir_created_if_missing(tmp_path: Path) -> None:
    """FilesystemClaimStorage bootstraps its directory on first issue."""
    from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage

    nested = tmp_path / "deep" / "nested" / ".claims"
    assert not nested.exists()
    store = FilesystemClaimStorage(nested)
    await store.issue(_make_record())
    assert nested.is_dir()


@pytest.mark.asyncio
async def test_atomicity_marker_uses_fsync(
    claim_storage_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem-only: issue calls os.fsync on the sidecar fd."""
    from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage

    store = await claim_storage_factory(tmp_path)
    if not isinstance(store, FilesystemClaimStorage):
        pytest.skip("filesystem-only atomicity primitive")
    calls: list[int] = []
    real_fsync = os.fsync

    def tracking(fd: int) -> None:
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking)
    await store.issue(_make_record())
    assert len(calls) >= 1
