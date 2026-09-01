"""T039 — US4 regression: the revocation-check extension point (Brief 514)."""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from cassetta.auth import jwt_tokens

KEY = b"k" * 32


def _mint(claims_extra: dict[str, Any] | None = None) -> str:
    now = int(time.time())
    base = {
        "bundle_path": "inbox/alice/n.md",
        "bundle_id": str(uuid.uuid4()),
        "sender": "bob",
        "recipient": "alice",
        "mode": "inline",
        "manifest": {"file_count": 0, "files": []},
        "iat": now,
        "nbf": now,
        "exp": now + 300,
    }
    if claims_extra:
        base.update(claims_extra)
    return jwt_tokens.sign(base, key=KEY)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    jwt_tokens.set_revocation_check(lambda _jti: None)
    yield
    jwt_tokens.set_revocation_check(lambda _jti: None)


def test_default_hook_accepts_any_jti() -> None:
    token = _mint()
    jwt_tokens.verify(token, primary=KEY, secondary=None)


def test_custom_hook_rejects_matching_jti() -> None:
    token = _mint()
    decoded = jwt_tokens.verify(token, primary=KEY, secondary=None)
    revoked_jti = decoded["jti"]

    def _hook(jti: str) -> None:
        if jti == revoked_jti:
            raise jwt_tokens.RevokedTokenError(jti)

    jwt_tokens.set_revocation_check(_hook)
    with pytest.raises(jwt_tokens.RevokedTokenError):
        jwt_tokens.verify(token, primary=KEY, secondary=None)


def test_hook_does_not_run_when_verify_fails_earlier() -> None:
    """Revocation hook must NOT run if signature verification fails first."""
    calls: list[str] = []

    def _hook(jti: str) -> None:
        calls.append(jti)

    jwt_tokens.set_revocation_check(_hook)
    token = _mint()
    with pytest.raises(jwt_tokens.TokenInvalidSignature):
        jwt_tokens.verify(token, primary=b"z" * 32, secondary=None)
    assert calls == []  # hook never called when sig fails
