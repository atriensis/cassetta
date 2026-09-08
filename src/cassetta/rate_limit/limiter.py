"""In-process rate limiter, fan-out cap helper, and counter-emission glue.

The module owns:

- A single ``slowapi.Limiter`` instance keyed off ``get_remote_address``
  (which honours uvicorn's ``--forwarded-allow-ips`` substitution per R3).
  Backed by the ``memory://`` storage; per-pod state, lost on restart.
- ``_record_rate_limit_hit`` — emits ``cassetta.rate_limit.hits`` to the
  configured ``MetricsProvider`` (one increment per rejection, paired
  one-to-one with the 429 / MCP error payload).
- ``_check_fanout_cap`` + ``FanoutCapExceeded`` — content-based fan-out
  rejection raised before any storage write.
- ``check_rate_limit_imperative`` — the imperative entry point used by
  the MCP ``cassetta_broadcast`` tool body (the per-tool-call boundary
  that the HTTP-level decorator cannot reach). The caller names the route
  its traffic belongs to; the helper records that name on the request.
- ``recorded_rate_limit_route`` — reads it back. The 429 handler counts
  what the caller said rather than guessing from the request path, so a
  route this library does not serve is still attributed correctly.

The module is owned by the open core, and a downstream distribution reaches
it through ``request.app.state.limiter`` only — nothing downstream needs to
import an internal symbol from here beyond this module's public re-exports
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

# The counters a rejection can be attributed to. Deliberately still two names
# rather than a free string: the alphabet is the seam, and a caller naming its
# own route is not the same as a caller inventing a metric tag.
RateLimitRoute = Literal["onboard", "broadcast"]

# Where the route travels between the helper and the exception handler. It lives
# on the request's own scope state, which Starlette shares across every
# ``Request`` built on that scope — so the handler's request sees what the route
# handler's request recorded, and nothing leaks between requests.
_ROUTE_STATE_ATTR = "cassetta_rate_limit_route"

# What a rejection that never passed through the helper is counted as. A
# decorator-raised one is the case: it cannot record anything, and this is what
# the handler attributed it to before the route became the caller's to name.
_DEFAULT_ROUTE: RateLimitRoute = "broadcast"


def recorded_rate_limit_route(request: Request) -> RateLimitRoute:
    """The route recorded for this request, or ``broadcast`` if none was."""
    route = getattr(request.state, _ROUTE_STATE_ATTR, None)
    if route == "onboard" or route == "broadcast":
        return route
    return _DEFAULT_ROUTE


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
    route: RateLimitRoute,
    reason: Literal["rate", "fanout_cap"],
) -> None:
    """Emit ``cassetta.rate_limit.hits`` against the configured provider.

    Routed through ``safe_emit`` so the broken-``MetricsProvider``
    posture matches every other counter call site.
    """
    from cassetta.structured_log import safe_emit

    metrics = request.app.state.backends.metrics_provider
    safe_emit(
        metric_name="cassetta.rate_limit.hits",
        metric_tags={"route": route, "reason": reason},
        metrics=metrics,
    )


def check_rate_limit_imperative(
    request: Request,
    rate_string: str,
    *,
    route: RateLimitRoute,
) -> None:
    """Drive the shared limiter bucket from a non-decorator call site.

    Used by the MCP ``cassetta_broadcast`` tool body — HTTP middleware
    cannot count per-tool-call traffic on a long-lived MCP/SSE request,
    so the tool calls into this helper at its entry point.

    ``route`` names the counter this traffic belongs to, and the caller is
    the one that knows: this library serves ``broadcast``, and an
    application embedding it serves routes this library has never heard
    of. The name is recorded on the request *before* the bucket is
    consulted, so the :class:`RateLimitExceeded` raised below is already
    attributable by the time the app-level handler sees it.

    Raises :class:`RateLimitExceeded` (slowapi's exception) when the
    bucket would overflow. The MCP tool body MUST translate it into a
    ``ValueError`` whose message is the JSON object
    ``{"error": "rate_limit", "retry_after": <seconds>}``.
    """
    setattr(request.state, _ROUTE_STATE_ATTR, route)
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
    "RateLimitRoute",
    "_check_fanout_cap",
    "_record_rate_limit_hit",
    "check_rate_limit_imperative",
    "limiter",
    "recorded_rate_limit_route",
]
