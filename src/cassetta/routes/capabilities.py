"""REST endpoint — ``GET /capabilities`` (Brief 516).

Returns the same ``CapabilitiesDocument`` shape the MCP tool
``cassetta_capabilities`` returns. Read-only, side-effect-free.

See ``specs/516-advertise-limits-handshake/contracts/capabilities-rest.md``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from cassetta.auth import get_current_identity
from cassetta.capabilities import build_capabilities_document
from cassetta.dependencies import get_limits_policy
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import LimitsPolicy, PolicyContext
from cassetta.structured_log import safe_emit

router = APIRouter()


@router.get("/capabilities")
async def get_capabilities(
    request: Request,
    identity: Annotated[Identity, Depends(get_current_identity)],
    policy: Annotated[LimitsPolicy, Depends(get_limits_policy)],
) -> dict[str, object]:
    """Return the server's advertised capabilities document."""
    doc = await build_capabilities_document(
        policy,
        PolicyContext(identity=identity),
        via="rest",
    )
    # Brief 533 FR-008: capability query counter (via=rest). Dev-mode
    # (no-auth) MUST still increment per Edge Cases #93.
    safe_emit(
        metric_name="cassetta.capabilities.queries",
        metric_tags={"via": "rest"},
        metrics=request.app.state.backends.metrics_provider,
    )
    return doc
