"""Layer-2 default — `DefaultAccessPolicy`.

Allow-all policy. Returns True for every decision. This preserves the pre-504
behavior of core-only deployments: as long as the caller has authenticated,
every storage / inbox / admin operation is permitted.
"""

from __future__ import annotations

from typing import ClassVar

from cassetta.protocols.identity import Identity


class DefaultAccessPolicy:
    """Allow-all policy — the core default."""

    kind: ClassVar[str] = "core"

    async def check(
        self,
        identity: Identity,
        resource: str,
        action: str,
    ) -> bool:
        return True

    async def visible_agents(
        self,
        identity: Identity,
        labels: list[str],
    ) -> list[str]:
        return list(labels)
