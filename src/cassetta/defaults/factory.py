"""Brief 518 / 520 — Layer 3 factory for core default implementations.

Constitution §VII ("Three-Layer Architecture, NON-NEGOTIABLE") requires
Layer 2 (``cassetta.app``) to never name a specific vendor. This module
is the single Layer 3 home for constructing the nine core default
implementations that ``app.py`` uses when the caller of ``create_app``
does not inject their own.

Adding a new core default is a two-file change: add a field to
:class:`BackendConfig` and a line to :func:`build_core_defaults`, plus
whatever Layer 3 backend module implements the new Protocol. ``app.py``
is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage
from cassetta.backends.filesystem.keystore import FileKeyStore
from cassetta.backends.filesystem.storage import FilesystemBackend
from cassetta.config import AppConfig
from cassetta.defaults.default_access import DefaultAccessPolicy
from cassetta.defaults.default_alias import DefaultAliasResolver
from cassetta.defaults.default_identity import DefaultIdentityProvider
from cassetta.defaults.default_limits import CoreLimitsPolicy
from cassetta.defaults.default_metrics import DefaultMetricsProvider
from cassetta.defaults.default_transport import CoreReferenceTransport
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.alias import AliasResolver
from cassetta.protocols.claim_storage import ClaimStorage
from cassetta.protocols.identity import IdentityProvider
from cassetta.protocols.keystore import KeyStoreProtocol
from cassetta.protocols.limits import LimitsPolicy
from cassetta.protocols.metrics import MetricsProvider
from cassetta.protocols.reference_transport import ReferenceTransport
from cassetta.protocols.storage import StorageBackend


@dataclass(frozen=True)
class BackendConfig:
    """Immutable bundle of the nine Protocol-typed core defaults."""

    backend: StorageBackend
    key_store: KeyStoreProtocol
    identity_provider: IdentityProvider
    access_policy: AccessPolicy
    alias_resolver: AliasResolver
    limits_policy: LimitsPolicy
    metrics_provider: MetricsProvider
    reference_transport: ReferenceTransport
    claim_store: ClaimStorage


def build_core_defaults(config: AppConfig) -> BackendConfig:
    """Construct the core reference defaults from ``config``.

    The ``key_store`` instance is constructed once and shared with
    ``DefaultAliasResolver``, preserving the same-instance wiring that
    ``app.py`` relied on pre-refactor.
    """
    key_store = FileKeyStore(keys_file=config.keys_file)
    return BackendConfig(
        backend=FilesystemBackend(root_path=config.storage_path),
        key_store=key_store,
        identity_provider=DefaultIdentityProvider(),
        access_policy=DefaultAccessPolicy(),
        alias_resolver=DefaultAliasResolver(key_store=key_store),
        limits_policy=CoreLimitsPolicy(config.limits),
        metrics_provider=DefaultMetricsProvider(),
        reference_transport=CoreReferenceTransport(config.public_base_url),
        claim_store=FilesystemClaimStorage(
            Path(config.storage_path) / ".claims"
        ),
    )
