"""Capabilities document assembly — a Layer-2 helper.

Turns a :class:`LimitsPolicy` into the six-field ``CapabilitiesDocument``
returned by the MCP tool ``cassetta_capabilities`` and the REST endpoint
``GET /capabilities``. Pure glue — reads the policy, emits one DEBUG
log event, returns a dict. No I/O.
"""

from __future__ import annotations

import logging

from cassetta import __version__ as _package_version
from cassetta.protocols.limits import LimitsPolicy, PolicyContext
from cassetta.structured_log import struct_log

logger = logging.getLogger("cassetta")


SCHEMA_VERSION: int = 1
SUPPORTED_MODES: tuple[str, str, str] = ("inline", "batch", "reference")
FEATURES: tuple[str, ...] = ("peek", "batch_upload", "reference_download", "rest_send_init")


#: Product version — single source of truth is ``cassetta.__version__``
#: (updated atomically with ``pyproject.toml`` by ``make release``).
#: ``importlib.metadata`` was tried first and dropped: editable installs
#: lag behind in-source bumps until ``uv sync``, silently producing
#: ``server_version`` mismatches after every release bump.
SERVER_VERSION: str = _package_version


async def build_capabilities_document(
    policy: LimitsPolicy,
    ctx: PolicyContext,
    *,
    via: str,
) -> dict[str, object]:
    """Build the wire-level ``CapabilitiesDocument`` for a given policy + caller.

    Args:
        policy: The configured :class:`LimitsPolicy`. Its
            ``advertise_limits(ctx)`` and ``ttls(ctx)`` methods are
            called on every invocation — no server-side memoisation.
        ctx: The caller's :class:`PolicyContext`. In core the returned
            document is identity-independent; cloud policies may vary
            by tier.
        via: ``"mcp"``, ``"rest"``, or ``"cli"`` — discriminator that
            flows into the DEBUG log so the three surfaces can be told
            apart in a mixed log stream.

    Returns:
        A dict with exactly six top-level keys in the canonical order
        ``schema_version, server_version, supported_modes, limits,
        ttls, features``.
    """
    limits = policy.advertise_limits(ctx)
    ttls = policy.ttls(ctx)
    features = await policy.advertise_features(ctx)
    doc: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "server_version": SERVER_VERSION,
        "supported_modes": list(SUPPORTED_MODES),
        "limits": dict(limits),
        "ttls": dict(ttls),
        "features": features,
    }
    struct_log(
        logger,
        logging.DEBUG,
        "capabilities.queried",
        identity_label=ctx.identity.label,
        detail={"via": via},
    )
    return doc
