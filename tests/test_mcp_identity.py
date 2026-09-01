"""Tests for MCP middleware identity plumbing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cassetta.auth import KeyInfo
from cassetta.defaults.default_identity import DefaultIdentityProvider
from cassetta.mcp_server import (
    get_current_identity,
    get_sender_label,
    set_current_identity,
    set_sender_label,
)
from cassetta.protocols.identity import Identity


class TestMCPIdentityContextVar:
    @pytest.mark.asyncio
    async def test_set_and_get_identity(self) -> None:
        identity = Identity(label="alice", extra={"user_id": "u-1"})
        set_current_identity(identity)
        got = get_current_identity()
        assert got is identity
        assert got.extra["user_id"] == "u-1"

    @pytest.mark.asyncio
    async def test_sender_label_still_works(self) -> None:
        set_sender_label("legacy-agent")
        assert get_sender_label() == "legacy-agent"

    @pytest.mark.asyncio
    async def test_identity_and_sender_label_coexist(self) -> None:
        identity = Identity(label="bob")
        set_current_identity(identity)
        set_sender_label("bob")
        assert get_current_identity() is identity
        assert get_sender_label() == "bob"

    @pytest.mark.asyncio
    async def test_identity_resolution_via_default_provider(self) -> None:
        provider = DefaultIdentityProvider()
        info = KeyInfo(
            label="test-key",
            key_prefix="cst_test",
            created_at=datetime.now(UTC),
            is_active=True,
        )
        identity = await provider.resolve(info)
        set_current_identity(identity)
        assert get_current_identity().label == "test-key"
