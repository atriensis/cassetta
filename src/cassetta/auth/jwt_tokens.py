"""HS256 JWT sign/verify for Cassetta upload credentials (Brief 514).

Wraps pyjwt with rotation-aware verification (primary + optional secondary
key) and a module-level revocation-check extension point keyed on ``jti``.

The default revocation check is a no-op; deployments that want active
revocation (cloud, multi-pod) install their own callable via
:func:`set_revocation_check`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import jwt as _jwt
from jwt.exceptions import (
    ExpiredSignatureError,
    ImmatureSignatureError,
    InvalidSignatureError,
    InvalidTokenError,
    MissingRequiredClaimError,
)

_ALG = "HS256"
_REQUIRED_CLAIMS: list[str] = ["exp", "nbf", "iat", "jti"]


class TokenError(Exception):
    """Base class for all JWT verification failures surfaced by this module."""


class TokenExpired(TokenError):
    """JWT ``exp`` is in the past."""


class TokenNotYetValid(TokenError):
    """JWT ``nbf`` is in the future (clock skew exceeds leeway)."""


class TokenInvalidSignature(TokenError):
    """HMAC signature does not match any configured signing key."""


class TokenMissingClaim(TokenError):
    """A required claim is missing from the decoded payload."""

    def __init__(self, claim: str) -> None:
        super().__init__(f"missing required claim: {claim}")
        self.claim = claim


class TokenDecodeError(TokenError):
    """Structural decode failure (malformed JWT)."""


class RevokedTokenError(TokenError):
    """The revocation-check hook rejected this ``jti``."""

    def __init__(self, jti: str) -> None:
        super().__init__(f"token revoked: jti={jti}")
        self.jti = jti


def _noop_revocation_check(_jti: str) -> None:
    return None


_revocation_check: Callable[[str], None] = _noop_revocation_check


def set_revocation_check(fn: Callable[[str], None]) -> None:
    """Install a revocation-check callable invoked during :func:`verify`.

    The callable receives the decoded ``jti`` claim and MUST raise
    :class:`RevokedTokenError` when the token is to be rejected. Any other
    exception is treated as an infrastructure error and propagates as-is.
    The default hook is a no-op.
    """
    global _revocation_check
    _revocation_check = fn


def sign(claims: dict[str, Any], *, key: bytes) -> str:
    """Sign ``claims`` with HS256 using ``key``.

    A fresh ``jti`` (UUIDv4) is injected unconditionally — callers SHOULD NOT
    pre-populate it, but a caller-supplied value is preserved if present.
    """
    payload = dict(claims)
    if not payload.get("jti"):
        payload["jti"] = str(uuid.uuid4())
    return _jwt.encode(payload, key, algorithm=_ALG)


def _decode_with(token: str, key: bytes) -> dict[str, Any]:
    return _jwt.decode(
        token,
        key,
        algorithms=[_ALG],
        options={"require": _REQUIRED_CLAIMS},
    )


def verify(
    token: str,
    *,
    primary: bytes,
    secondary: bytes | None,
) -> dict[str, Any]:
    """Verify ``token`` against ``primary`` (fallback: ``secondary``).

    On success, calls the revocation hook with the decoded ``jti`` and
    returns the decoded payload. Every pyjwt-level failure is mapped onto a
    :class:`TokenError` subclass so callers do not import pyjwt.
    """
    try:
        decoded = _decode_with(token, primary)
    except InvalidSignatureError:
        if secondary is None:
            raise TokenInvalidSignature("signature mismatch") from None
        try:
            decoded = _decode_with(token, secondary)
        except InvalidSignatureError as exc:
            raise TokenInvalidSignature("signature mismatch") from exc
        except ExpiredSignatureError as exc:
            raise TokenExpired("token expired") from exc
        except ImmatureSignatureError as exc:
            raise TokenNotYetValid("token not yet valid (nbf)") from exc
        except MissingRequiredClaimError as exc:
            raise TokenMissingClaim(str(exc.claim)) from exc
        except InvalidTokenError as exc:
            raise TokenDecodeError(str(exc)) from exc
    except ExpiredSignatureError as exc:
        raise TokenExpired("token expired") from exc
    except ImmatureSignatureError as exc:
        raise TokenNotYetValid("token not yet valid (nbf)") from exc
    except MissingRequiredClaimError as exc:
        raise TokenMissingClaim(str(exc.claim)) from exc
    except InvalidTokenError as exc:
        raise TokenDecodeError(str(exc)) from exc

    jti = str(decoded.get("jti") or "")
    # Extension point: revocation check keyed to jti lands here
    _revocation_check(jti)
    return decoded
