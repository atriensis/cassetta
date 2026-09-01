"""ASGI middleware for MCP endpoint authentication.

Wraps the MCP ASGI app to validate Bearer tokens via KeyStore
before requests reach the MCP handler.
"""

import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import FastAPI

from cassetta.auth.observability import emit_auth_failure

logger = logging.getLogger("cassetta")

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class MCPAuthMiddleware:
    """ASGI middleware that validates Bearer API keys for the MCP endpoint.

    Reads key_store and dev_mode from the FastAPI app.state at request time,
    so it works correctly even when mounted before lifespan runs.
    """

    def __init__(self, app: ASGIApp, fastapi_app: FastAPI) -> None:
        self._app = app
        self._fastapi_app = fastapi_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        from cassetta.mcp_server import set_current_identity, set_sender_label
        from cassetta.protocols.identity import Identity

        # Read auth config from app state at request time
        dev_mode: bool = getattr(self._fastapi_app.state, "dev_mode", False)

        if dev_mode:
            # Dev mode: do NOT touch the sender label or current identity.
            # Existing test fixtures pre-populate these for MCP flows that
            # run without real authentication. The MCP server's `_enforce`
            # helper provides a dev Identity stand-in when the policy is
            # called with no identity present. We still expose a dev
            # identity in the ASGI scope for audit / logging purposes.
            dev_identity = Identity(label="dev", extra={"dev": True})
            scope["mcp_identity"] = dev_identity
            await self._app(scope, receive, send)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()

        backends = self._fastapi_app.state.backends

        if not auth_value.startswith("Bearer "):
            emit_auth_failure(
                metrics=backends.metrics_provider,
                source="mcp",
                reason="missing_bearer",
                identity_hint=None,
            )
            await self._send_401(send, "Missing authentication credentials")
            return

        token = auth_value[7:]  # Strip "Bearer "
        key_store = backends.key_store
        key_info = await key_store.validate(token)
        if key_info is None:
            emit_auth_failure(
                metrics=backends.metrics_provider,
                source="mcp",
                reason="invalid_key",
                identity_hint=token[:12] if token else None,
            )
            await self._send_401(send, "Invalid or revoked API key")
            return

        # Store key info in scope for audit logging
        scope["mcp_key_info"] = key_info

        # Resolve identity via the pluggable provider
        identity = await backends.identity_provider.resolve(key_info)
        scope["mcp_identity"] = identity
        set_current_identity(identity)
        set_sender_label(identity.label)

        await self._app(scope, receive, send)

    @staticmethod
    async def _send_401(send: Send, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )
