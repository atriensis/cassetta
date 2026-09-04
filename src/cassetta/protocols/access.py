"""Layer-1 protocol — Access policy extension point.

Core defines `AccessPolicy` (Protocol). Cloud deployments plug a real
implementation (e.g. a team-membership-based policy from the cloud package).
The default allow-all implementation `DefaultAccessPolicy` lives in
`cassetta.defaults.default_access`.

Core never interprets policy rules — it only calls
`policy.check(identity, resource, action)` for per-resource decisions and
`policy.visible_agents(identity, labels)` for list-filter decisions at every
storage, inbox, and administrative code path.

Resource descriptors follow `"{namespace}:{object_id}"`; actions are lowercase
single words (`read`, `write`, `list`, `delete`, `pick`, `create`, `revoke`,
etc.).
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from cassetta.protocols.identity import Identity


@runtime_checkable
class AccessPolicy(Protocol):
    """Strategy interface for authorization decisions.

    Given a resolved caller identity, a resource descriptor, and an action,
    return a boolean decision. Implementations MUST be stateless with respect
    to the call (any state lives in injected dependencies).
    """

    kind: ClassVar[str]

    async def check(
        self,
        identity: Identity,
        resource: str,
        action: str,
    ) -> bool: ...

    async def visible_agents(
        self,
        identity: Identity,
        labels: list[str],
    ) -> list[str]:
        """Return the subset of `labels` the caller is permitted to see.

        Input order is preserved in the output. Implementations MUST be
        stateless with respect to the call. Callers MUST treat exceptions as
        fail-closed (return empty list).
        """
        ...
