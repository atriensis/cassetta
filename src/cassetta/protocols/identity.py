"""Layer-1 protocol — Identity types for pluggable caller resolution.

Core defines `Identity` (label + opaque extra) and `IdentityProvider` (Protocol).
Cloud provides richer implementations that resolve user/team info. The default
implementation `DefaultIdentityProvider` lives in `cassetta.defaults.default_identity`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

from cassetta.auth import KeyInfo


@dataclass
class Identity:
    """Resolved caller identity.

    Core sees only `label`. The `extra` dict is opaque — core passes it
    through to logs and sender metadata but never reads specific keys.
    """

    label: str
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class IdentityProvider(Protocol):
    """Strategy interface for resolving API key info into caller identity."""

    kind: ClassVar[str]

    async def resolve(self, key_info: KeyInfo) -> Identity: ...
