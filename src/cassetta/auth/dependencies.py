import logging
from typing import TYPE_CHECKING, Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cassetta.auth.models import KeyInfo
from cassetta.auth.observability import Reason, emit_auth_failure

if TYPE_CHECKING:
    from cassetta.protocols.identity import Identity, IdentityProvider
    from cassetta.protocols.keystore import KeyStoreProtocol

http_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger("cassetta.auth")


def get_key_store(request: Request) -> "KeyStoreProtocol":
    """FastAPI dependency to get the KeyStore instance."""
    key_store = request.app.state.backends.key_store
    return cast("KeyStoreProtocol", key_store)


async def get_current_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)],
    request: Request,
) -> "Identity":
    """FastAPI dependency for unified authentication.

    Accepts either an ``X-Setup-Token`` header or an ``Authorization: Bearer``
    header and always produces an :class:`Identity`. Precedence when both
    are present: setup-token wins (protects admin bootstrap from accidental
    bearer-key shadowing).

    - Dev mode -> dev identity with ``extra["dev"]=True``
    - Valid setup token -> operator identity with ``extra["operator"]=True``
    - Valid bearer key -> identity resolved via the pluggable IdentityProvider
    - Neither / invalid -> 401
    """
    from cassetta.protocols.identity import Identity

    dev_mode: bool = request.app.state.dev_mode
    if dev_mode:
        identity = Identity(label="dev", extra={"dev": True})
        request.state.identity = identity
        return identity

    # Setup-token path wins if both are supplied.
    x_setup_token = request.headers.get("X-Setup-Token")
    setup_token: str = request.app.state.setup_token
    if x_setup_token is not None and x_setup_token == setup_token:
        identity = Identity(label="operator", extra={"operator": True})
        request.state.identity = identity
        return identity

    # Bearer-token path.
    if credentials is not None:
        key_store = get_key_store(request)
        info = await key_store.validate(credentials.credentials)
        if info is not None:
            identity_provider: IdentityProvider = request.app.state.backends.identity_provider
            identity = await identity_provider.resolve(info)
            # Attach the raw key info so callers that need it (e.g. keys.py)
            # can still reach it without a second lookup.
            request.state.key_info = info
            request.state.identity = identity
            return identity

    # No valid credentials present — emit a single auth.failure event.
    # Setup-token wins on reason precedence (matches the success-path
    # precedence: setup-token is checked before bearer).
    reason: Reason
    identity_hint: str | None
    if x_setup_token is not None:
        reason = "invalid_setup_token"
        identity_hint = None
    elif credentials is not None:
        reason = "invalid_key"
        identity_hint = credentials.credentials[:12] if credentials.credentials else None
    else:
        reason = "missing_bearer"
        identity_hint = None
    emit_auth_failure(
        metrics=request.app.state.backends.metrics_provider,
        source="rest",
        reason=reason,
        identity_hint=identity_hint,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid authentication credentials",
    )


async def get_current_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)],
    request: Request,
) -> KeyInfo | None:
    """Backwards-compat wrapper.

    Thin shim over :func:`get_current_identity` that returns the validated
    :class:`KeyInfo` when the caller authenticated via bearer token, and
    ``None`` in dev mode. Returns None when the caller is an operator
    (setup-token path) -- existing key-management routes do not need KeyInfo
    in that case.

    New code SHOULD depend on :func:`get_current_identity` directly.
    """
    from cassetta.protocols.identity import Identity

    dev_mode: bool = request.app.state.dev_mode
    if dev_mode:
        request.state.identity = Identity(label="dev", extra={"dev": True})
        return None

    identity = await get_current_identity(credentials, request)
    if identity.extra.get("operator"):
        return None
    return getattr(request.state, "key_info", None)


def require_setup_token(request: Request, x_setup_token: str | None = None) -> None:
    """Deprecated: use :func:`get_current_identity` + policy.check instead.

    Retained for backwards compatibility with callers that have not yet
    migrated. Does NOT run in dev mode. New admin routes must NOT depend
    on this -- they depend on :func:`get_current_identity` and call
    :meth:`AccessPolicy.check`.
    """
    dev_mode: bool = request.app.state.dev_mode
    if dev_mode:
        return

    setup_token: str = request.app.state.setup_token
    token = x_setup_token or request.headers.get("X-Setup-Token")
    if token != setup_token:
        emit_auth_failure(
            metrics=request.app.state.backends.metrics_provider,
            source="rest",
            reason="invalid_setup_token",
            identity_hint=None,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid setup token",
        )
