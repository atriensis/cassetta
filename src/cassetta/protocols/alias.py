"""Layer-1 protocol — Alias resolution for recipient addressing.

Core defines `ResolvedRecipient` (dataclass) and `AliasResolver` (Protocol).
The default implementation `DefaultAliasResolver` lives in
`cassetta.defaults.default_alias`. Cloud provides `CloudAliasResolver`
with user/team alias lookup and multicast fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable


@dataclass
class ResolvedRecipient:
    """Result of alias resolution.

    ``inbox_targets`` contains one or more storage-key prefixes where
    incoming files should be placed. A single-element list represents
    unicast; multiple elements represent multicast (e.g. team delivery).

    ``display_name`` is optional — used for logging and UX only.
    """

    inbox_targets: list[str]
    display_name: str | None = None


@runtime_checkable
class AliasResolver(Protocol):
    """Strategy interface for resolving recipient names to inbox targets.

    Returns ``ResolvedRecipient`` on success, ``None`` when the name
    cannot be resolved (unknown recipient).
    """

    kind: ClassVar[str]

    async def resolve(
        self, name: str, *, sender_label: str | None = None,
    ) -> ResolvedRecipient | None: ...
