"""Unit tests for _cassetta_peek — purity, TTL, authz (brief 513 US1)."""

from __future__ import annotations

import json
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage
from cassetta.backends.filesystem.keystore import FileKeyStore
from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.config import AppConfig, LimitsConfig
from cassetta.defaults.default_access import DefaultAccessPolicy
from cassetta.defaults.default_alias import DefaultAliasResolver
from cassetta.defaults.default_identity import DefaultIdentityProvider
from cassetta.defaults.default_limits import CoreLimitsPolicy
from cassetta.defaults.default_metrics import DefaultMetricsProvider
from cassetta.defaults.default_transport import CoreReferenceTransport
from cassetta.defaults.factory import BackendConfig
from cassetta.mcp_server import _cassetta_peek, configure, set_current_identity
from cassetta.protocols.identity import Identity


@pytest.fixture
def storage_root() -> str:
    return tempfile.mkdtemp()


@pytest.fixture
def backend(storage_root: str) -> FilesystemBackend:
    return FilesystemBackend(root_path=storage_root)


def _base_config(default_ttl: int = 0) -> AppConfig:
    return AppConfig(
        setup_token="",
        dev_mode=True,
        storage_path="",
        keys_file="",
        default_ttl=default_ttl,
        allowed_path_chars=r"a-zA-Z0-9\-_./",
        mcp_allowed_hosts=(),
        limits=LimitsConfig(),
    )


async def _write_bundle(
    backend: FilesystemBackend,
    path: str,
    files: list[tuple[str, bytes]],
    *,
    sender: str | None = None,
    created_at: datetime | None = None,
) -> dict:
    writer = await backend.open_bundle_write(path)
    try:
        import io

        records: list[dict] = []
        for name, data in files:
            await writer.write_file(name, io.BytesIO(data))
            records.append({"name": name, "size": len(data), "mime": "text/plain"})
        created = created_at or datetime.now(UTC)
        meta = {
            "schema_version": 1,
            "bundle_id": "b_" + name,
            "sender": sender,
            "created_at": created.isoformat(),
            "content_type": "application/octet-stream",
            "file_count": len(records),
            "files": records,
        }
        await writer.commit(meta)
        return meta
    except Exception:
        await writer.abort()
        raise


def _configure(
    backend: FilesystemBackend,
    config: AppConfig,
    *,
    access_policy=None,
) -> None:
    # FileKeyStore treats a missing file as empty; mkstemp would create an
    # empty file that fails JSON parse. Point at a non-existent path inside
    # a fresh temp directory instead.
    key_store = FileKeyStore(keys_file=str(Path(tempfile.mkdtemp()) / "keys.json"))
    backends = BackendConfig(
        backend=backend,
        key_store=key_store,
        identity_provider=DefaultIdentityProvider(),
        access_policy=access_policy or DefaultAccessPolicy(),
        alias_resolver=DefaultAliasResolver(key_store=key_store),
        limits_policy=CoreLimitsPolicy(config.limits),
        metrics_provider=DefaultMetricsProvider(),
        reference_transport=CoreReferenceTransport(""),
        claim_store=FilesystemClaimStorage(Path(tempfile.mkdtemp()) / ".claims"),
    )
    configure(config, backends)
    set_current_identity(Identity(label="alice"))


@pytest.mark.asyncio
async def test_peek_returns_meta(backend: FilesystemBackend) -> None:
    await _write_bundle(
        backend,
        "store/notes.md",
        [("notes.md", b"hello")],
        sender=None,
    )
    _configure(backend, _base_config())

    raw = await _cassetta_peek("notes.md")
    payload = json.loads(raw)
    assert "bundle" in payload
    assert payload["bundle"]["file_count"] == 1
    assert payload["bundle"]["files"][0]["name"] == "notes.md"


@pytest.mark.asyncio
async def test_peek_does_not_open_bytes(
    backend: FilesystemBackend,
) -> None:
    await _write_bundle(backend, "store/doc", [("x.md", b"body")])
    _configure(backend, _base_config())

    spy = MagicMock(side_effect=AssertionError("open_bundle_file_read invoked"))
    original = backend.open_bundle_file_read
    backend.open_bundle_file_read = spy  # type: ignore[method-assign]
    try:
        for _ in range(100):
            await _cassetta_peek("doc")
    finally:
        backend.open_bundle_file_read = original  # type: ignore[method-assign]
    assert spy.call_count == 0


@pytest.mark.asyncio
async def test_peek_does_not_change_mtime(
    backend: FilesystemBackend,
    storage_root: str,
) -> None:
    await _write_bundle(backend, "store/stable", [("a.md", b"x")])
    _configure(backend, _base_config())

    bundle_dir = Path(storage_root) / "data" / "store" / "stable"
    time.sleep(0.1)
    before = bundle_dir.stat().st_mtime

    for _ in range(100):
        await _cassetta_peek("stable")

    after = bundle_dir.stat().st_mtime
    assert before == after, "bundle directory mtime must not change"


@pytest.mark.asyncio
async def test_peek_orphan_raises_not_found(
    backend: FilesystemBackend,
    storage_root: str,
) -> None:
    # Create a bundle-shaped directory without meta.json
    orphan_dir = Path(storage_root) / "data" / "store" / "orphan"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "f.txt").write_bytes(b"x")
    _configure(backend, _base_config())

    with pytest.raises(ValueError, match="^Not found: orphan$"):
        await _cassetta_peek("orphan")


@pytest.mark.asyncio
async def test_peek_ttl_expired_does_not_delete(
    backend: FilesystemBackend,
    storage_root: str,
) -> None:
    old = datetime.now(UTC) - timedelta(seconds=60)
    await _write_bundle(
        backend,
        "store/old",
        [("a.md", b"x")],
        created_at=old,
    )
    _configure(backend, _base_config(default_ttl=10))

    bundle_dir = Path(storage_root) / "data" / "store" / "old"
    assert bundle_dir.is_dir()

    with pytest.raises(ValueError, match="^Not found: old$"):
        await _cassetta_peek("old")

    # Peek must NOT lazy-delete expired bundles.
    assert bundle_dir.is_dir(), "peek deleted an expired bundle; should not"
    assert (bundle_dir / "meta.json").is_file()


class _DenyAll:
    async def check(self, identity, resource, action) -> bool:  # noqa: ANN001
        return False


@pytest.mark.asyncio
async def test_peek_denied_by_access_policy(backend: FilesystemBackend) -> None:
    await _write_bundle(backend, "store/private", [("p.md", b"x")])
    _configure(backend, _base_config(), access_policy=_DenyAll())

    with pytest.raises(ValueError, match="Forbidden"):
        await _cassetta_peek("private")


@pytest.mark.asyncio
async def test_peek_default_access_policy_allows(
    backend: FilesystemBackend,
) -> None:
    await _write_bundle(backend, "store/ok", [("p.md", b"x")])
    _configure(backend, _base_config(), access_policy=DefaultAccessPolicy())

    raw = await _cassetta_peek("ok")
    assert "bundle" in json.loads(raw)


@pytest.mark.asyncio
async def test_peek_inbox_prefix(backend: FilesystemBackend) -> None:
    await _write_bundle(
        backend,
        "inbox/alice/notes.md",
        [("notes.md", b"hi")],
        sender="bob",
    )
    _configure(backend, _base_config())

    raw = await _cassetta_peek("inbox/alice/notes.md")
    payload = json.loads(raw)
    assert payload["bundle"]["sender"] == "bob"


@pytest.mark.asyncio
async def test_peek_store_prefix(backend: FilesystemBackend) -> None:
    await _write_bundle(backend, "store/p", [("p.md", b"x")])
    _configure(backend, _base_config())

    raw = await _cassetta_peek("store/p")
    payload = json.loads(raw)
    assert payload["bundle"]["file_count"] == 1


@pytest.mark.asyncio
async def test_peek_invalid_path(backend: FilesystemBackend) -> None:
    _configure(backend, _base_config())
    with pytest.raises(ValueError, match="^Invalid path:"):
        await _cassetta_peek("../etc/passwd")
