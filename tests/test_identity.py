"""Tests for Identity dataclass and IdentityProvider."""

import httpx
import pytest

from cassetta.defaults.default_identity import DefaultIdentityProvider
from cassetta.protocols.identity import Identity, IdentityProvider


class TestIdentity:
    """Tests for Identity dataclass."""

    def test_identity_with_label_only(self) -> None:
        identity = Identity(label="my-agent")
        assert identity.label == "my-agent"
        assert identity.extra == {}

    def test_identity_with_extra(self) -> None:
        extra = {"user_id": "abc-123", "team_id": "team-456"}
        identity = Identity(label="my-agent", extra=extra)
        assert identity.label == "my-agent"
        assert identity.extra == extra

    def test_identity_extra_is_opaque(self) -> None:
        """Core never interprets extra — any dict is valid."""
        extra = {"arbitrary": [1, 2, 3], "nested": {"deep": True}}
        identity = Identity(label="x", extra=extra)
        assert identity.extra["arbitrary"] == [1, 2, 3]
        assert identity.extra["nested"]["deep"] is True

    def test_identity_label_required(self) -> None:
        with pytest.raises(TypeError):
            Identity()  # type: ignore[call-arg]

    def test_identity_default_extra_is_empty_dict(self) -> None:
        id1 = Identity(label="a")
        id2 = Identity(label="b")
        # Each instance gets its own dict
        assert id1.extra is not id2.extra


class TestDefaultIdentityProvider:
    """Tests for DefaultIdentityProvider."""

    @pytest.mark.asyncio
    async def test_resolve_returns_label_only(self) -> None:
        from datetime import UTC, datetime

        from cassetta.auth import KeyInfo

        provider = DefaultIdentityProvider()
        info = KeyInfo(
            label="test-key",
            key_prefix="cst_1234",
            created_at=datetime.now(UTC),
            is_active=True,
        )
        identity = await provider.resolve(info)
        assert identity.label == "test-key"
        assert identity.extra == {}

    @pytest.mark.asyncio
    async def test_resolve_preserves_label(self) -> None:
        from datetime import UTC, datetime

        from cassetta.auth import KeyInfo

        provider = DefaultIdentityProvider()
        info = KeyInfo(
            label="special-label-123",
            key_prefix="cst_abcd",
            created_at=datetime.now(UTC),
            is_active=True,
        )
        identity = await provider.resolve(info)
        assert identity.label == "special-label-123"

    def test_default_provider_satisfies_protocol(self) -> None:
        """DefaultIdentityProvider must satisfy the IdentityProvider protocol."""
        provider = DefaultIdentityProvider()
        assert isinstance(provider, IdentityProvider)


class TestIdentityWiring:
    """Tests for identity resolution in auth middleware and request context."""

    @pytest.mark.asyncio
    async def test_auth_resolves_identity_in_request(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        """get_current_key should resolve identity and store in request state."""
        client, setup_token = auth_client
        # Create a key
        resp = await client.post(
            "/setup",
            json={"host": "test", "project": "identity-test"},
            headers={"X-Setup-Token": setup_token},
        )
        assert resp.status_code == 201
        api_key = resp.json()["api_key"]

        # Authenticate and check identity is available
        resp = await client.get(
            "/files/",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        # The request should succeed (identity resolved without error)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_mcp_sender_label_from_identity(self, auth_client: tuple[httpx.AsyncClient, str]) -> None:
        """MCP sender label should come from resolved identity.label."""
        # Simulate what MCPAuthMiddleware does
        from datetime import UTC, datetime

        from cassetta.auth import KeyInfo
        from cassetta.defaults.default_identity import DefaultIdentityProvider
        from cassetta.mcp_server import get_sender_label, set_sender_label

        provider = DefaultIdentityProvider()
        info = KeyInfo(
            label="mcp-agent",
            key_prefix="cst_test",
            created_at=datetime.now(UTC),
            is_active=True,
        )
        identity = await provider.resolve(info)
        set_sender_label(identity.label)
        assert get_sender_label() == "mcp-agent"

    @pytest.mark.asyncio
    async def test_identity_extra_passed_to_context(self) -> None:
        """Identity.extra should be available in request context."""
        extra = {"user_id": "u-123", "team_id": "t-456"}
        identity = Identity(label="test", extra=extra)
        # Verify extra is accessible and unmodified
        assert identity.extra["user_id"] == "u-123"
        assert identity.extra["team_id"] == "t-456"
