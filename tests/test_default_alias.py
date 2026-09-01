"""Tests for DefaultAliasResolver — core label-only resolution."""

import pytest

from cassetta.auth import KeyInfo
from cassetta.defaults.default_alias import DefaultAliasResolver


class FakeKeyStore:
    """Minimal KeyStore for testing alias resolution."""

    def __init__(self, keys: list[KeyInfo] | None = None) -> None:
        self._keys = keys or []

    @property
    def setup_done(self) -> bool:
        return True

    async def setup(self, label: str) -> tuple[str, KeyInfo]:
        raise NotImplementedError

    async def create_key(self, label: str) -> tuple[str, KeyInfo]:
        raise NotImplementedError

    async def validate(self, raw_key: str) -> KeyInfo | None:
        return None

    async def list_keys(self) -> list[KeyInfo]:
        return self._keys

    async def revoke_key(self, label: str) -> None:
        raise NotImplementedError

    async def rotate_key(self, label: str) -> tuple[str, KeyInfo]:
        raise NotImplementedError

    def is_healthy(self) -> bool:
        return True


def _make_key(label: str, *, active: bool = True) -> KeyInfo:
    from datetime import UTC, datetime

    return KeyInfo(
        label=label,
        key_prefix="cst_test1234",
        created_at=datetime.now(UTC),
        is_active=active,
    )


class TestDefaultAliasResolver:
    @pytest.mark.asyncio
    async def test_label_with_colon_resolves(self) -> None:
        keys = [_make_key("laptop:docs")]
        resolver = DefaultAliasResolver(key_store=FakeKeyStore(keys))
        result = await resolver.resolve("laptop:docs")
        assert result is not None
        assert result.inbox_targets == ["inbox/laptop:docs/"]
        assert result.display_name == "laptop:docs"

    @pytest.mark.asyncio
    async def test_label_without_colon_returns_none(self) -> None:
        keys = [_make_key("laptop:docs")]
        resolver = DefaultAliasResolver(key_store=FakeKeyStore(keys))
        result = await resolver.resolve("bob")
        assert result is None

    @pytest.mark.asyncio
    async def test_nonexistent_label_returns_none(self) -> None:
        keys = [_make_key("laptop:docs")]
        resolver = DefaultAliasResolver(key_store=FakeKeyStore(keys))
        result = await resolver.resolve("server:api")
        assert result is None

    @pytest.mark.asyncio
    async def test_revoked_key_returns_none(self) -> None:
        keys = [_make_key("laptop:docs", active=False)]
        resolver = DefaultAliasResolver(key_store=FakeKeyStore(keys))
        result = await resolver.resolve("laptop:docs")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_keystore_returns_none(self) -> None:
        resolver = DefaultAliasResolver(key_store=FakeKeyStore([]))
        result = await resolver.resolve("laptop:docs")
        assert result is None

    @pytest.mark.asyncio
    async def test_protocol_conformance(self) -> None:
        from cassetta.protocols.alias import AliasResolver

        resolver = DefaultAliasResolver(key_store=FakeKeyStore([]))
        assert isinstance(resolver, AliasResolver)
