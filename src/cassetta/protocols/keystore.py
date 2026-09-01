"""Layer-1 protocol — `KeyStoreProtocol`.

See specs/505-architecture-refactoring/contracts/key_store.md for the full
contract documentation. The concrete implementations live in
`cassetta.backends.filesystem.keystore.FileKeyStore` (core) and
cloud vendor-specific key store (cloud package).
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from cassetta.auth import KeyInfo


@runtime_checkable
class KeyStoreProtocol(Protocol):
    """Abstract interface for API key management."""

    kind: ClassVar[str]

    @property
    def setup_done(self) -> bool: ...

    async def setup(self, label: str) -> tuple[str, KeyInfo]: ...

    async def create_key(
        self,
        label: str,
        *,
        user_id: str | None = None,
    ) -> tuple[str, KeyInfo]: ...

    async def validate(self, raw_key: str) -> KeyInfo | None: ...

    async def list_keys(self) -> list[KeyInfo]: ...

    async def revoke_key(self, label: str) -> None: ...

    async def rotate_key(self, label: str) -> tuple[str, KeyInfo]: ...

    def is_healthy(self) -> bool: ...
