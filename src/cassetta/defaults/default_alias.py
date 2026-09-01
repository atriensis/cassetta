"""Default alias resolver — label-only resolution for core deployments.

Names containing ``:``, are treated as key labels and verified against the
KeyStore. Names without ``:`` return ``None`` (core has no alias concept).
"""

from __future__ import annotations

from typing import ClassVar

from cassetta.protocols.alias import ResolvedRecipient
from cassetta.protocols.keystore import KeyStoreProtocol


class DefaultAliasResolver:
    """Core-only resolver: resolves ``host:project`` labels, nothing else."""

    kind: ClassVar[str] = "core"

    def __init__(self, key_store: KeyStoreProtocol) -> None:
        self._key_store = key_store

    async def resolve(
        self, name: str, *, sender_label: str | None = None,
    ) -> ResolvedRecipient | None:
        if ":" not in name:
            return None  # Core has no alias concept

        # Verify the key label exists and is active
        keys = await self._key_store.list_keys()
        for k in keys:
            if k.label == name and k.is_active:
                return ResolvedRecipient(
                    inbox_targets=[f"inbox/{name}/"],
                    display_name=name,
                )
        return None  # Label not found or revoked
