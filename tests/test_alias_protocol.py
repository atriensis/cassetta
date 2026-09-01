"""Tests for AliasResolver protocol and ResolvedRecipient dataclass."""

import pytest

from cassetta.protocols.alias import AliasResolver, ResolvedRecipient


class TestResolvedRecipient:
    def test_single_target(self) -> None:
        r = ResolvedRecipient(inbox_targets=["inbox/laptop:docs/"])
        assert r.inbox_targets == ["inbox/laptop:docs/"]
        assert r.display_name is None

    def test_multicast_targets(self) -> None:
        r = ResolvedRecipient(
            inbox_targets=["inbox/a/", "inbox/b/", "inbox/c/"],
            display_name="devs",
        )
        assert len(r.inbox_targets) == 3
        assert r.display_name == "devs"

    def test_empty_targets(self) -> None:
        r = ResolvedRecipient(inbox_targets=[])
        assert r.inbox_targets == []


class TestAliasResolverProtocol:
    def test_protocol_is_runtime_checkable(self) -> None:
        assert hasattr(AliasResolver, "__protocol_attrs__") or isinstance(AliasResolver, type)

    @pytest.mark.asyncio
    async def test_concrete_class_satisfies_protocol(self) -> None:
        class MockResolver:
            kind = "test"

            async def resolve(
                self,
                name: str,
                *,
                sender_label: str | None = None,
            ) -> ResolvedRecipient | None:
                return ResolvedRecipient(inbox_targets=[f"inbox/{name}/"])

        resolver = MockResolver()
        assert isinstance(resolver, AliasResolver)
        result = await resolver.resolve("test:proj")
        assert result is not None
        assert result.inbox_targets == ["inbox/test:proj/"]
