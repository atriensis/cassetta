"""Data type + re-exports for reference-mode download claims.

The ``ClaimStorage`` Protocol and its filesystem implementation moved
to ``cassetta.protocols.claim_storage`` and
``cassetta.backends.filesystem.claim_storage``; they are
re-exported here for backwards-compatible imports. ``ClaimRecord``
stays here because it is data, not protocol, and ~15 test sites
import it from this module directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cassetta.protocols.claim_storage import (
    BundleClaimedError,
    ClaimStorage,
)

if TYPE_CHECKING:
    from cassetta.backends.filesystem.claim_storage import (
        FilesystemClaimStorage,
    )


@dataclass(frozen=True)
class ClaimRecord:
    """Persisted state for one active reference-mode download claim."""

    schema_version: int
    jti: str
    bundle_path: str
    bundle_id: str
    recipient: str
    created_at: str
    files_fetched: list[str] = field(default_factory=list)


def _iso_to_epoch(iso: str) -> float:
    """Parse an ISO-8601 UTC timestamp into epoch seconds."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def __getattr__(name: str) -> Any:
    """Lazy re-export: resolve ``FilesystemClaimStorage`` on first access.

    Deferring the import (PEP 562) breaks the
    ``cassetta.backends.filesystem.claim_storage`` → ``cassetta.claims``
    circular that otherwise triggers when anything loads
    ``cassetta.defaults.factory`` before ``cassetta.claims`` — e.g. via the
    ``cassetta/__init__.py`` public re-exports. The
    backend module imports ``ClaimRecord`` / ``_iso_to_epoch`` from here,
    so if we eagerly imported it here too, neither side would be fully
    initialised when the other tried to read from it.
    """
    if name == "FilesystemClaimStorage":
        from cassetta.backends.filesystem import claim_storage as _cs

        return getattr(_cs, name)
    raise AttributeError(f"module 'cassetta.claims' has no attribute {name!r}")


__all__ = [
    "BundleClaimedError",
    "ClaimRecord",
    "ClaimStorage",
    "FilesystemClaimStorage",
]
