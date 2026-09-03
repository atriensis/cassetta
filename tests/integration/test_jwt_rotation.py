"""Signing-key rotation without downtime."""

from __future__ import annotations

import base64
import time
import uuid

import pytest

from cassetta.auth import jwt_tokens

KEY_A = b"A" * 36
KEY_B = b"B" * 36


def _mint(key: bytes, exp_offset: int = 300) -> str:
    now = int(time.time())
    claims = {
        "bundle_path": "inbox/alice/rot.md",
        "bundle_id": str(uuid.uuid4()),
        "sender": "bob",
        "recipient": "alice",
        "mode": "inline",
        "manifest": {"file_count": 0, "files": []},
        "iat": now,
        "nbf": now,
        "exp": now + exp_offset,
    }
    return jwt_tokens.sign(claims, key=key)


def test_phase_1_only_A_accepted() -> None:
    """Before rotation: only A-signed tokens verify under primary=A."""
    token = _mint(KEY_A)
    jwt_tokens.verify(token, primary=KEY_A, secondary=None)
    token_b = _mint(KEY_B)
    with pytest.raises(jwt_tokens.TokenInvalidSignature):
        jwt_tokens.verify(token_b, primary=KEY_A, secondary=None)


def test_phase_2_B_primary_A_secondary_both_accepted() -> None:
    """Mid-rotation: primary=B, secondary=A — both A- and B-signed verify."""
    a_tok = _mint(KEY_A)
    b_tok = _mint(KEY_B)
    jwt_tokens.verify(a_tok, primary=KEY_B, secondary=KEY_A)
    jwt_tokens.verify(b_tok, primary=KEY_B, secondary=KEY_A)


def test_phase_3_only_B_accepted_after_drop_A() -> None:
    """Post-rotation: A dropped — only B-signed tokens verify."""
    a_tok = _mint(KEY_A)
    b_tok = _mint(KEY_B)
    jwt_tokens.verify(b_tok, primary=KEY_B, secondary=None)
    with pytest.raises(jwt_tokens.TokenInvalidSignature):
        jwt_tokens.verify(a_tok, primary=KEY_B, secondary=None)


def test_encoded_length_matches_32_bytes_or_more() -> None:
    """Sanity: both rotation keys satisfy the ≥32-byte HS256 minimum."""
    assert len(KEY_A) >= 32
    assert len(KEY_B) >= 32
    base64.b64encode(KEY_A)  # sanity
    base64.b64encode(KEY_B)
