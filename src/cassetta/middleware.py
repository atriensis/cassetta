"""ASGI middleware for observability — request ID and metrics.

``RequestIdMiddleware``: generates a UUID4 per request, stores it in
``request.state.request_id`` and the ``request_id_var`` ContextVar so
that all logging calls during the request carry the same identifier.
Also records request start time for duration calculation and emits
request-level metrics (total, duration, errors) via MetricsProvider.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cassetta.structured_log import new_request_id, request_id_var, safe_emit


class RequestIdMiddleware:
    """ASGI middleware — request ID generation + request-level metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        rid = new_request_id()
        start = time.monotonic()
        token = request_id_var.set(rid)

        # Stash request_id in scope state so route handlers can access it
        # via request.state.request_id.
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["request_id"] = rid
        scope["state"]["_start_time"] = start

        status_code: int = 200

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 200)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Emit request-level metrics if a provider is wired on the bundle.
            app_state: Any = scope.get("app")
            state = getattr(app_state, "state", None)
            backends = getattr(state, "backends", None) if state else None
            metrics = (
                getattr(backends, "metrics_provider", None) if backends else None
            )

            if metrics is not None:
                duration = time.monotonic() - start
                path = scope.get("path", "unknown")
                method = scope.get("method", "UNKNOWN")
                tags = {"method": method, "path": path}

                safe_emit(
                    metric_name="cassetta.requests.total",
                    metric_tags=tags,
                    metrics=metrics,
                )
                # ``observe`` is not currently routed through ``safe_emit``
                # because the helper covers only counter/gauge surfaces;
                # the audit grep (test_audit_no_raw_metric_calls) ignores
                # ``observe`` for the same reason.
                try:
                    metrics.observe(
                        "cassetta.request.duration_seconds", duration, tags=tags,
                    )
                except Exception:
                    pass

                if status_code >= 400:
                    error_tags = {
                        **tags,
                        "status": f"{status_code // 100}xx",
                    }
                    safe_emit(
                        metric_name="cassetta.request.errors",
                        metric_tags=error_tags,
                        metrics=metrics,
                    )

            request_id_var.reset(token)
