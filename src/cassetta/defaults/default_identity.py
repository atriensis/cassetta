"""Layer-2 default — `DefaultIdentityProvider`.

Returns label-only identity. Used by core-only deployments and as the
fallback inside `create_app` when no identity provider is supplied.
"""

from __future__ import annotations

from typing import ClassVar

from cassetta.auth import KeyInfo
from cassetta.protocols.identity import Identity


class DefaultIdentityProvider:
    """Returns label-only identity — default for core-only deployments."""

    kind: ClassVar[str] = "core"

    async def resolve(self, key_info: KeyInfo) -> Identity:
        return Identity(label=key_info.label)
