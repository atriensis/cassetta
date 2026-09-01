"""In-process rate limiter, fan-out cap helper, and counter-emission glue.

Brief 531 — Operational resilience. The module owns:

- A single ``slowapi.Limiter`` instance keyed off ``get_remote_address``
  (which honours uvicorn's ``--forwarded-allow-ips`` substitution per R3).
  Backed by the ``memory://`` storage; per-pod state, lost on restart.
- ``_record_rate_limit_hit`` — emits ``cassetta.rate_limit.hits`` to the
  configured ``MetricsProvider`` (one increment per rejection, paired
  one-to-one with the 429 / MCP error payload).
- ``_check_fanout_cap`` + ``FanoutCapExceeded`` — content-based fan-out
  rejection raised before any storage write (FR-007).
- ``check_rate_limit_imperative`` — the imperative entry point used by
  the MCP ``cassetta_broadcast`` tool body (the per-tool-call boundary
  that the HTTP-level decorator cannot reach).

The module is owned by the open core; ``cloud/`` reaches it via
``request.app.state.limiter`` only — no ``cloud → core`` import of any
internal symbol is needed beyond this module's public re-exports
(Constitution V).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from limits import RateLimitItem, parse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.wrappers import Limit

if TYPE_CHECKING:
    from starlette.requests import Request


limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")


class FanoutCapExceeded(Exception):
    """Raised when a broadcast target list exceeds ``broadcast_max_targets``.

    The unified app-level handler maps this to HTTP 429 with the
    ``reason=fanout_cap`` envelope (REST surfaces); the MCP broadcast tool
    catches it locally and re-raises as a structured ``ValueError``.
    """

    def __init__(self, max_targets: int) -> None:
        super().__init__(f"fanout_cap_exceeded: max_targets={max_targets}")
        self.max_targets = max_targets


def _check_fanout_cap(target_count: int, max_targets: int) -> None:
    """Raise :class:`FanoutCapExceeded` when ``target_count > max_targets``."""
    if target_count > max_targets:
        raise FanoutCapExceeded(max_targets=max_targets)


def _record_rate_limit_hit(
    request: Request,
    *,
    route: Literal["onboard", "broadcast"],
    reason: Literal["rate", "fanout_cap"],
) -> None:
    """Emit ``cassetta.rate_limit.hits`` against the configured provider.

    Brief 533 FR-063: routed through ``safe_emit`` so the broken-
    ``MetricsProvider`` posture matches every other counter call site.
    """
    from cassetta.structured_log import safe_emit
    metrics = request.app.state.backends.metrics_provider
    safe_emit(
        metric_name="cassetta.rate_limit.hits",
        metric_tags={"route": route, "reason": reason},
        metrics=metrics,
    )


def check_rate_limit_imperative(request: Request, rate_string: str) -> None:
    """Drive the shared limiter bucket from a non-decorator call site.

    Used by the MCP ``cassetta_broadcast`` tool body — HTTP middleware
    cannot count per-tool-call traffic on a long-lived MCP/SSE request,
    so the tool calls into this helper at its entry point.

    Raises :class:`RateLimitExceeded` (slowapi's exception) when the
    bucket would overflow. The MCP tool body MUST translate it to a
    structured ``ValueError`` payload per
    ``contracts/rate-limit-rejection.md``.
    """
    parsed: RateLimitItem = parse(rate_string)
    key = get_remote_address(request)
    if not limiter._limiter.hit(parsed, key):
        # Wrap the parsed item in slowapi's Limit so RateLimitExceeded
        # constructs cleanly — its __init__ touches `limit.error_message`.
        wrapped = Limit(
            limit=parsed,
            key_func=get_remote_address,
            scope=None,
            per_method=False,
            methods=None,
            error_message=None,
            exempt_when=None,
            cost=1,
            override_defaults=True,
        )
        raise RateLimitExceeded(wrapped)


__all__ = [
    "FanoutCapExceeded",
    "RateLimitExceeded",
    "_check_fanout_cap",
    "_record_rate_limit_hit",
    "check_rate_limit_imperative",
    "limiter",
]
