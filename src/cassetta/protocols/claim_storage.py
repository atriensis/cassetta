"""Layer 1 — ``ClaimStorage`` Protocol for download-claim persistence.

Abstracts the vendor-specific concurrency primitive that makes
``issue()`` atomic across writers. Layer 2 product code (``app.py``,
``downloads.py``, ``gc.py``, ``mcp_server.py``, ``routes/``) annotates
this Protocol; concrete implementations live in Layer 3
(``backends/filesystem/claim_storage.py`` and the cloud
``backends/azure/claim_storage.py``).

See ``specs/517-claimstore-cloud-parity/contracts/claim-storage-protocol.md``
for the full method contracts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from cassetta.claims import ClaimRecord


class BundleClaimedError(Exception):
    """Raised by ``ClaimStorage.issue`` when another writer won the race.

    Callers MUST translate this into the same "bundle not found" error
    surface a late-comer would see — never expose the race itself
    (FR-011a).
    """

    def __init__(self, bundle_path: str) -> None:
        super().__init__(f"bundle already claimed: {bundle_path}")
        self.bundle_path = bundle_path


@runtime_checkable
class ClaimStorage(Protocol):
    """Abstract contract for download-claim storage.

    Implementations provide a vendor-specific concurrency primitive
    that makes ``issue()`` atomic (FR-008 — "only one pick wins").
    """

    kind: ClassVar[str]

    async def issue(self, claim: ClaimRecord) -> None:
        """Atomically claim ``claim.bundle_path`` and persist the record.

        Raises
        ------
        BundleClaimedError
            Another active claim for the same ``bundle_path`` exists.
        """
        ...

    async def get(self, jti: str) -> ClaimRecord | None:
        """Return the record by jti, or None if absent.

        Raises ``ValueError`` on a malformed persisted body.
        """
        ...

    async def mark_fetched(self, jti: str, name: str) -> ClaimRecord:
        """Append ``name`` to files_fetched (idempotent)."""
        ...

    async def delete(self, jti: str) -> None:
        """Idempotent removal of the claim and its lock artefact."""
        ...

    def iter_all(self) -> AsyncIterator[ClaimRecord]:
        """Yield every well-formed record. Malformed entries WARN + skip."""
        ...

    async def iter_active_by_bundle_path(
        self,
        ttl_s: int,
    ) -> Mapping[str, str]:
        """Build ``{bundle_path: jti}`` of non-expired claims."""
        ...
