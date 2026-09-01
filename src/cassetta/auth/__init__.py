"""Auth package -- public surface for FastAPI deps and key DTOs."""

from typing import TYPE_CHECKING, Any

from cassetta.auth.dependencies import (
    get_current_identity,
    get_current_key,
    get_key_store,
    http_bearer,
    require_setup_token,
)
from cassetta.auth.models import KEY_PREFIX, KEY_PREFIX_LEN, KeyInfo, KeyRecord

if TYPE_CHECKING:
    from cassetta.backends.filesystem.keystore import (
        FileKeyStore,
    )

    KeyStore = FileKeyStore


def __getattr__(name: str) -> Any:
    """Lazy re-export to avoid the ``auth`` ↔ ``keystore`` circular.

    ``cassetta.backends.filesystem.keystore`` imports ``KeyInfo`` /
    ``KeyRecord`` from ``cassetta.auth.models``. Importing the submodule
    initialises the ``cassetta.auth`` package, so if this ``__init__.py``
    eagerly pulled ``FileKeyStore`` back from the Layer 3 module we would
    re-enter a partially-loaded ``keystore`` module — fatal. Deferring
    via PEP 562 sidesteps the cycle while preserving
    ``from cassetta.auth import FileKeyStore`` / ``KeyStore``.
    """
    if name in {"FileKeyStore", "KeyStore"}:
        from cassetta.backends.filesystem import keystore as _ks

        return _ks.FileKeyStore
    raise AttributeError(f"module 'cassetta.auth' has no attribute {name!r}")


__all__ = [
    "KEY_PREFIX",
    "KEY_PREFIX_LEN",
    "FileKeyStore",
    "KeyInfo",
    "KeyRecord",
    "KeyStore",
    "get_current_identity",
    "get_current_key",
    "get_key_store",
    "http_bearer",
    "require_setup_token",
]
