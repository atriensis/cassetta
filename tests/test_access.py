"""Tests for AccessPolicy Protocol and DefaultAccessPolicy."""

from __future__ import annotations

import pytest

from cassetta.defaults.default_access import DefaultAccessPolicy
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.identity import Identity


class TestDefaultAccessPolicy:
    @pytest.mark.asyncio
    async def test_allows_any_resource_and_action(self) -> None:
        policy = DefaultAccessPolicy()
        identity = Identity(label="anyone")

        assert await policy.check(identity, "files:foo.txt", "read") is True
        assert await policy.check(identity, "inbox:bob", "write") is True
        assert await policy.check(identity, "keys:alice", "create") is True
        assert await policy.check(identity, "invites:team-x", "revoke") is True
        assert await policy.check(identity, "users:*", "list") is True

    @pytest.mark.asyncio
    async def test_allows_with_rich_extra(self) -> None:
        policy = DefaultAccessPolicy()
        identity = Identity(
            label="ops",
            extra={
                "user_id": "u-1",
                "operator": False,
                "teams": [{"team_id": "t-1", "role": "member"}],
            },
        )
        assert await policy.check(identity, "files:shared/doc.md", "write") is True

    def test_satisfies_protocol(self) -> None:
        policy = DefaultAccessPolicy()
        assert isinstance(policy, AccessPolicy)

    @pytest.mark.asyncio
    async def test_identity_extra_is_opaque_to_policy(self) -> None:
        policy = DefaultAccessPolicy()
        # Arbitrary shapes pass through
        weird_extras: list[dict[str, object]] = [
            {},
            {"user_id": None},
            {"deep": {"nested": [1, 2, 3]}},
            {"operator": True},
        ]
        for extra in weird_extras:
            identity = Identity(label="x", extra=extra)
            assert await policy.check(identity, "files:foo", "read") is True


class TestVisibleAgentsProtocolConformance:
    """AccessPolicy gains a visible_agents method."""

    def test_default_access_policy_satisfies_extended_protocol(self) -> None:
        # After T006 lands, DefaultAccessPolicy must still satisfy the
        # runtime_checkable AccessPolicy Protocol with the new method.
        policy = DefaultAccessPolicy()
        assert isinstance(policy, AccessPolicy)
        assert hasattr(policy, "visible_agents")
        assert callable(policy.visible_agents)
