"""Centralised emission helper for ``auth.failure`` events (Brief 529).

Single function ``emit_auth_failure`` invoked from every authentication
401/403 site (REST, MCP, download). Wraps both the structured-log call
and the metrics-counter increment in best-effort exception handling so
that observability faults never mask the auth path's response — the
401/403 must reach the client even if the metrics provider or log
handler raises (FR-012 / Brief 529).

Brief 533 (FR-063) extends the same independent-best-effort posture to
all other counter call sites via :func:`cassetta.structured_log.safe_emit`.
This helper PRE-DATES ``safe_emit`` and is the auth-specific specialisation
of the same pattern; the public fallback string
``"auth observability emission failed"`` is regression-locked by Brief 529's
test suite (SC-011 / FR-067) and therefore cannot be replaced by
``safe_emit``'s per-step fallback strings ``"struct_log failed for ..."`` /
``"metrics.increment failed for ..."``. The audit grep
(``test_audit_no_raw_metric_calls``) explicitly EXEMPTS this file for that
reason — the raw ``metrics.increment`` call below is the documented
fenced-exception case from contract C-SAFEEMIT-006.
"""

from __future__ import annotations

import logging
from typing import Literal

from cassetta.protocols.metrics import MetricsProvider
from cassetta.structured_log import struct_log

Source = Literal["rest", "mcp", "download"]
Reason = Literal[
    "missing_bearer",
    "invalid_key",
    "invalid_setup_token",
    "jwt_invalid",
    "jwt_expired",
    "jwt_aud_mismatch",
]

_logger = logging.getLogger("cassetta.auth")


def emit_auth_failure(
    *,
    metrics: MetricsProvider,
    source: Source,
    reason: Reason,
    identity_hint: str | None = None,
) -> None:
    """Emit one ``auth.failure`` event + one ``cassetta.auth.failures`` increment.

    Best-effort: any exception from ``struct_log`` or
    ``metrics.increment`` is caught; a fallback ``logger.exception``
    records the failure with the legacy Brief-529 message. If that
    fallback also raises (catastrophic broken-logging case), the helper
    swallows so the caller's 401/403 is preserved.
    """
    assert identity_hint is None or len(identity_hint) <= 12, (
        "identity_hint must be <= 12 chars (FR-006)"
    )
    try:
        struct_log(
            _logger,
            logging.WARNING,
            "auth.failure",
            detail={
                "source": source,
                "reason": reason,
                "identity_hint": identity_hint,
            },
        )
        metrics.increment(
            "cassetta.auth.failures",
            tags={"source": source, "reason": reason},
        )
    except Exception:
        try:
            _logger.exception("auth observability emission failed")
        except Exception:
            pass
