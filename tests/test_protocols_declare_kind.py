"""Every Layer-1 Protocol declares a ``kind: ClassVar[str]`` attribute, and
every concrete implementation in this package sets it to a well-formed label.

Scope note: this asserts the core half only. Implementations that live outside
this package assert their own halves, and the cross-family uniqueness check that
spans both (``filesystem`` vs another backend never colliding) belongs downstream,
where both sides are importable.
"""

from __future__ import annotations

import re

import pytest

from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage
from cassetta.backends.filesystem.keystore import FileKeyStore
from cassetta.backends.filesystem.storage import FilesystemBackend
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

KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

PROTOCOL_CLASSES: list[type] = [
    AccessPolicy,
    AliasResolver,
    ClaimStorage,
    IdentityProvider,
    KeyStoreProtocol,
    LimitsPolicy,
    MetricsProvider,
    ReferenceTransport,
    StorageBackend,
]

CONCRETE_IMPLS: list[tuple[type, str]] = [
    (FilesystemBackend, "filesystem"),
    (FileKeyStore, "filesystem"),
    (FilesystemClaimStorage, "filesystem"),
    (DefaultIdentityProvider, "core"),
    (DefaultAccessPolicy, "core"),
    (DefaultAliasResolver, "core"),
    (DefaultMetricsProvider, "core"),
    (CoreLimitsPolicy, "core"),
    (CoreReferenceTransport, "core"),
]

FAMILY_KINDS: dict[type, list[str]] = {
    StorageBackend: ["filesystem"],
    KeyStoreProtocol: ["filesystem"],
    ClaimStorage: ["filesystem"],
    IdentityProvider: ["core"],
    AccessPolicy: ["core"],
    AliasResolver: ["core"],
    MetricsProvider: ["core"],
    LimitsPolicy: ["core"],
    ReferenceTransport: ["core"],
}


@pytest.mark.parametrize("proto_cls", PROTOCOL_CLASSES)
def test_protocols_declare_kind_attribute(proto_cls: type) -> None:
    assert "kind" in proto_cls.__annotations__, f"{proto_cls.__name__} does not declare `kind` in its class body."


@pytest.mark.parametrize("impl_cls,expected_kind", CONCRETE_IMPLS)
def test_concrete_impls_have_well_formed_kind(
    impl_cls: type,
    expected_kind: str,
) -> None:
    assert hasattr(impl_cls, "kind"), f"{impl_cls.__name__} is missing `kind`"
    assert isinstance(impl_cls.kind, str), f"{impl_cls.__name__}.kind is not a str: {type(impl_cls.kind)!r}"
    assert impl_cls.kind, f"{impl_cls.__name__}.kind is empty"
    assert KIND_PATTERN.match(impl_cls.kind), (
        f"{impl_cls.__name__}.kind={impl_cls.kind!r} violates pattern {KIND_PATTERN.pattern!r}"
    )
    assert impl_cls.kind == expected_kind, f"{impl_cls.__name__}.kind={impl_cls.kind!r} != {expected_kind!r}"


@pytest.mark.parametrize("proto,kinds", list(FAMILY_KINDS.items()))
def test_kind_unique_per_protocol_family(
    proto: type,
    kinds: list[str],
) -> None:
    assert len(kinds) == len(set(kinds)), f"Duplicate `kind` within {proto.__name__} family: {kinds}"
