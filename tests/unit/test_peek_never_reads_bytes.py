"""Peek-purity guard — asserts peek never opens bundle file bytes."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
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

# 36 bytes, above the 32 `config.py` requires of an HS256 key. Passed explicitly wherever this file
# builds an `AppConfig`: a config that can sign carries a key, in tests for the same reason as in
# production. See `tests/test_signing_key_fixtures.py`.
_TEST_JWT_KEY = b"test-test-test-test-test-test-test-t"


def _config() -> AppConfig:
    return AppConfig(
        setup_token="",
        dev_mode=True,
        storage_path="",
        keys_file="",
        default_ttl=0,
        allowed_path_chars=r"a-zA-Z0-9\-_./",
        mcp_allowed_hosts=(),
        jwt_primary_key=_TEST_JWT_KEY,
        limits=LimitsConfig(),
    )


async def _seed(backend: FilesystemBackend, path: str, name: str, data: bytes) -> None:
    import io

    writer = await backend.open_bundle_write(path)
    try:
        await writer.write_file(name, io.BytesIO(data))
        meta = {
            "schema_version": 1,
            "bundle_id": "b1",
            "sender": None,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": 1,
            "files": [{"name": name, "size": len(data), "mime": "text/plain"}],
        }
        await writer.commit(meta)
    except Exception:
        await writer.abort()
        raise


@pytest.mark.asyncio
async def test_peek_never_calls_open_bundle_file_read() -> None:
    """Regression guard against future refactors that might inline bytes reads."""
    storage = tempfile.mkdtemp()
    backend = FilesystemBackend(root_path=storage)
    await _seed(backend, "store/thing", "thing.txt", b"payload")

    spy = MagicMock(side_effect=AssertionError("peek must not read bundle bytes — open_bundle_file_read was called"))
    original = backend.open_bundle_file_read
    backend.open_bundle_file_read = spy  # type: ignore[method-assign]

    cfg = _config()
    # Point at a non-existent path inside a temp dir — tempfile.mkstemp creates
    # an empty file that breaks FileKeyStore's JSON load.
    key_store = FileKeyStore(keys_file=str(Path(tempfile.mkdtemp()) / "keys.json"))
    backends = BackendConfig(
        backend=backend,
        key_store=key_store,
        identity_provider=DefaultIdentityProvider(),
        access_policy=DefaultAccessPolicy(),
        alias_resolver=DefaultAliasResolver(key_store=key_store),
        limits_policy=CoreLimitsPolicy(cfg.limits),
        metrics_provider=DefaultMetricsProvider(),
        reference_transport=CoreReferenceTransport(""),
        claim_store=FilesystemClaimStorage(Path(tempfile.mkdtemp()) / ".claims"),
    )
    configure(cfg, backends)
    set_current_identity(Identity(label="auditor"))
    try:
        for _ in range(10):
            result = await _cassetta_peek("thing")
            assert "bundle" in json.loads(result)
    finally:
        backend.open_bundle_file_read = original  # type: ignore[method-assign]

    assert spy.call_count == 0
