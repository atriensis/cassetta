"""T007 — failing-first tests for ``cassetta.auth.jwt_tokens``.

Covers:
- HS256 sign + verify roundtrip with the primary key.
- Mapping of pyjwt exceptions to our ``TokenError`` hierarchy:
  ``TokenExpired``, ``TokenNotYetValid``, ``TokenInvalidSignature``,
  ``TokenMissingClaim``.
- Secondary-key rotation: tokens signed by secondary key are accepted.
- Unknown-key tokens are rejected.
- Every minted token carries a fresh ``jti`` (UUIDv4).
- Revocation-check extension point: default no-op, ``set_revocation_check``
  installs a user-provided callable, raising ``RevokedTokenError`` from the
  hook propagates.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from cassetta.auth import jwt_tokens

PRIMARY = b"p" * 32
SECONDARY = b"s" * 32
UNKNOWN = b"u" * 32


def _claims(exp_offset: int = 300, **overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "bundle_path": "inbox/alice/notes.md",
        "bundle_id": str(uuid.uuid4()),
        "sender": "bob",
        "recipient": "alice",
        "mode": "inline",
        "manifest": {
            "file_count": 1,
            "files": [{"name": "note.txt", "size": 5, "mime": "text/plain"}],
        },
        "iat": now,
        "nbf": now,
        "exp": now + exp_offset,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_revocation_check() -> Any:
    jwt_tokens.set_revocation_check(lambda _jti: None)
    yield
    jwt_tokens.set_revocation_check(lambda _jti: None)


def test_sign_verify_roundtrip_primary() -> None:
    token = jwt_tokens.sign(_claims(), key=PRIMARY)
    result = jwt_tokens.verify(token, primary=PRIMARY, secondary=None)
    assert result["bundle_path"] == "inbox/alice/notes.md"
    assert result["mode"] == "inline"
    assert "jti" in result and result["jti"]


def test_verify_expired_token_raises_expired() -> None:
    token = jwt_tokens.sign(_claims(exp_offset=-60), key=PRIMARY)
    with pytest.raises(jwt_tokens.TokenExpired):
        jwt_tokens.verify(token, primary=PRIMARY, secondary=None)


def test_verify_nbf_future_raises_not_yet_valid() -> None:
    now = int(time.time())
    claims = _claims()
    claims["nbf"] = now + 600
    token = jwt_tokens.sign(claims, key=PRIMARY)
    with pytest.raises(jwt_tokens.TokenNotYetValid):
        jwt_tokens.verify(token, primary=PRIMARY, secondary=None)


def test_verify_bad_signature_raises_invalid_signature() -> None:
    token = jwt_tokens.sign(_claims(), key=PRIMARY)
    with pytest.raises(jwt_tokens.TokenInvalidSignature):
        jwt_tokens.verify(token, primary=UNKNOWN, secondary=None)


@pytest.mark.parametrize("missing_claim", ["exp", "nbf", "jti"])
def test_verify_missing_required_claim_raises(missing_claim: str) -> None:
    import jwt

    claims = _claims()
    # Inject an explicit jti via sign to ensure deterministic removal.
    claims["jti"] = str(uuid.uuid4())
    claims.pop(missing_claim, None)
    # Forge a token without going through our sign (which adds jti).
    token = jwt.encode(claims, PRIMARY, algorithm="HS256")
    with pytest.raises(jwt_tokens.TokenMissingClaim):
        jwt_tokens.verify(token, primary=PRIMARY, secondary=None)


def test_secondary_key_accepted_on_primary_mismatch() -> None:
    token = jwt_tokens.sign(_claims(), key=SECONDARY)
    # New primary is different; secondary matches the signing key.
    result = jwt_tokens.verify(token, primary=PRIMARY, secondary=SECONDARY)
    assert result["bundle_path"] == "inbox/alice/notes.md"


def test_unknown_key_rejected_even_with_secondary() -> None:
    token = jwt_tokens.sign(_claims(), key=UNKNOWN)
    with pytest.raises(jwt_tokens.TokenInvalidSignature):
        jwt_tokens.verify(token, primary=PRIMARY, secondary=SECONDARY)


def test_jti_populated_unconditionally() -> None:
    claims_without_jti = _claims()
    claims_without_jti.pop("jti", None)
    token = jwt_tokens.sign(claims_without_jti, key=PRIMARY)
    decoded = jwt_tokens.verify(token, primary=PRIMARY, secondary=None)
    # UUIDv4 shape (36 chars with hyphens or 32 hex chars — be flexible).
    assert decoded["jti"], "jti must be populated unconditionally"
    # Parse as UUID — will raise if shape is wrong.
    uuid.UUID(decoded["jti"])


def test_default_revocation_check_is_no_op() -> None:
    # Arrange: just ensure module-level default doesn't raise for any jti.
    jwt_tokens.set_revocation_check(lambda _jti: None)
    token = jwt_tokens.sign(_claims(), key=PRIMARY)
    jwt_tokens.verify(token, primary=PRIMARY, secondary=None)


def test_custom_revocation_check_raises_revoked() -> None:
    token = jwt_tokens.sign(_claims(), key=PRIMARY)
    decoded = jwt_tokens.verify(token, primary=PRIMARY, secondary=None)
    revoked_jti = decoded["jti"]

    def _check(jti: str) -> None:
        if jti == revoked_jti:
            raise jwt_tokens.RevokedTokenError(jti)

    jwt_tokens.set_revocation_check(_check)
    with pytest.raises(jwt_tokens.RevokedTokenError):
        jwt_tokens.verify(token, primary=PRIMARY, secondary=None)


def test_revocation_check_does_not_affect_untargeted_jti() -> None:
    def _check(jti: str) -> None:
        if jti == "deliberately-nonsense-jti":
            raise jwt_tokens.RevokedTokenError(jti)

    jwt_tokens.set_revocation_check(_check)
    token = jwt_tokens.sign(_claims(), key=PRIMARY)
    jwt_tokens.verify(token, primary=PRIMARY, secondary=None)  # should not raise
