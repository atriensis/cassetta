"""Unit tests for DefaultAccessPolicy.visible_agents."""

from __future__ import annotations

import pytest

from cassetta.defaults.default_access import DefaultAccessPolicy
from cassetta.protocols.identity import Identity


class TestDefaultAccessPolicyVisibleAgents:
    @pytest.mark.asyncio
    async def test_passthrough_returns_all_labels_in_order(self) -> None:
        policy = DefaultAccessPolicy()
        identity = Identity(label="anyone")
        labels = ["alice", "bob", "carol", "dan"]
        result = await policy.visible_agents(identity, labels)
        assert result == labels

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_output(self) -> None:
        policy = DefaultAccessPolicy()
        identity = Identity(label="anyone")
        result = await policy.visible_agents(identity, [])
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_copy_not_input_reference(self) -> None:
        # Pit-of-success: caller mutating result should not mutate input.
        policy = DefaultAccessPolicy()
        identity = Identity(label="anyone")
        labels = ["alice", "bob"]
        result = await policy.visible_agents(identity, labels)
        result.pop()
        assert labels == ["alice", "bob"]

    @pytest.mark.asyncio
    async def test_passthrough_for_any_identity_extra(self) -> None:
        policy = DefaultAccessPolicy()
        for extra in [
            {},
            {"operator": True},
            {"user_id": "u-1", "teams": [{"team_id": "t-1"}]},
            {"deep": {"nested": [1, 2, 3]}},
        ]:
            identity = Identity(label="x", extra=extra)
            assert await policy.visible_agents(identity, ["a", "b"]) == ["a", "b"]
