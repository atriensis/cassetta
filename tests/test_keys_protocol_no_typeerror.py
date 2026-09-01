"""Brief 535 Fix 5 — ``KeyStoreProtocol.create_key`` accepts ``user_id``
unconditionally and both backends conform to the unified signature.

The companion AST regression lock that forbids ``try/except TypeError``
in ``routes/keys.py`` lives in ``test_layer2_uses_protocol_annotations.py``
where the Brief 518-520 layer-2 invariants already accumulate.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest

from cassetta.auth import KeyInfo
from cassetta.backends.filesystem.keystore import FileKeyStore
from cassetta.protocols.keystore import KeyStoreProtocol


def test_keystore_protocol_signature() -> None:
    """FR-016 — ``user_id`` is keyword-only with default ``None`` on the
    Protocol method."""
    sig = inspect.signature(KeyStoreProtocol.create_key)
    params = sig.parameters
    assert "user_id" in params, (
        f"Protocol.create_key missing user_id parameter: {sig}"
    )
    user_id_param = params["user_id"]
    assert user_id_param.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"user_id must be KEYWORD_ONLY, got {user_id_param.kind}"
    )
    assert user_id_param.default is None, (
        f"user_id must default to None, got {user_id_param.default!r}"
    )


@pytest.mark.asyncio
async def test_file_key_store_accepts_user_id_kwarg() -> None:
    """FR-016 — ``FileKeyStore.create_key`` accepts ``user_id`` without
    raising ``TypeError``; binding is silently ignored (single-user
    backend)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = FileKeyStore(Path(tmpdir) / "keys.json")
        raw_key, info = await store.create_key(
            "test-label", user_id="alice",
        )
        assert isinstance(raw_key, str)
        assert isinstance(info, KeyInfo)
        assert info.label == "test-label"
        # File backend has no per-key user binding to surface.
        assert not hasattr(info, "user_id") or getattr(info, "user_id", None) is None
