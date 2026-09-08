"""Layer-1 protocol — the configuration this library reads.

The other protocols in this package describe a *behaviour* a caller can substitute. This one
describes a *value* a caller supplies, and it exists for the same reason: an application embedding
this library holds its own settings object, and until this protocol there was no way to hand it
over. Every Layer-2 signature named the concrete ``AppConfig``, so the only configuration this
library could be given was the one it defined itself.

What the protocol declares *is* the boundary. It lists the settings something under ``src/``
actually reads, and nothing else — an attribute no code here consults is not part of this library's
configuration however plausible it looks on a dataclass, and ``tests/test_config_protocol.py``
fails on one. Two such attributes had accumulated: both were parsed, range-checked, carried on the
published object and documented in ``docs/CONFIG.md`` as operative settings, and neither governed
anything.

``AppConfig`` in ``src/cassetta/config.py`` is the reference implementation — what ``load_config()``
builds from the environment, and what a self-hosting deployment runs on. It is now *an*
implementation rather than *the* type.

Every member is a read-only property rather than a mutable attribute. That is the honest statement
— nothing here writes to a configuration object — and it is also the only shape a frozen dataclass
can satisfy: a settable protocol member demands a settable implementation, which would exclude
``AppConfig`` itself and every immutable settings type a caller is likely to bring.

Layer-1 invariants: no vendor imports, no I/O, and no runtime import of Layer 2. ``LimitsConfig``
is a concrete Layer-2 type and is named here as one, imported under ``TYPE_CHECKING`` so the arrow
points inward at runtime while mypy still sees the real type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from cassetta.config import LimitsConfig


@runtime_checkable
class CoreConfig(Protocol):
    """The settings this library reads, whoever supplies them.

    Sixteen values, grouped as an operator meets them in ``docs/CONFIG.md``.
    """

    kind: ClassVar[str]

    # Authentication and mode.

    @property
    def setup_token(self) -> str: ...

    @property
    def dev_mode(self) -> bool: ...

    # Storage layout and retention.

    @property
    def storage_path(self) -> str: ...

    @property
    def keys_file(self) -> str: ...

    @property
    def default_ttl(self) -> int: ...

    @property
    def allowed_path_chars(self) -> str: ...

    # Signing keys, and the rotation window the verify path honours.

    @property
    def jwt_primary_key(self) -> bytes: ...

    @property
    def jwt_secondary_key(self) -> bytes | None: ...

    @property
    def jwt_primary_key_source(self) -> str: ...

    @property
    def jwt_key_overlap_ttl(self) -> int: ...

    # Addressing, transport and logging.

    @property
    def public_base_url(self) -> str: ...

    @property
    def mcp_allowed_hosts(self) -> tuple[str, ...]: ...

    @property
    def log_format(self) -> str: ...

    # Upload/download policy inputs, and the shared broadcast budget.

    @property
    def limits(self) -> LimitsConfig: ...

    @property
    def rate_limit_broadcast(self) -> str: ...

    @property
    def broadcast_max_targets(self) -> int: ...
