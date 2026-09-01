"""Reference-mode download helpers (Brief 515).

Shared by MCP pick/get and the REST read routes so that the JWT-mint +
claim-sidecar-issue + envelope-build sequence has one source of truth
and the FR-011a strict ordering is encoded once.

- :func:`build_reference_payload_for_inbox` — used by inbox pick and
  inbox GET. Writes a claim sidecar (pick only — callers decide whether
  to pass ``write_claim=True``). Ordering: read meta → evaluate policy
  (done upstream) → build claim body + mint JWT in memory → fsync
  sidecar (if inbox + pick) → return credential. Credential never
  returned if sidecar write fails.
- :func:`build_reference_payload_for_store` — store get (no sidecar;
  FR-010a). Mints the credential, returns the envelope immediately.

Callers are responsible for:
- invoking this helper ONLY when the policy says ``mode: "reference"``
- mapping :class:`BundleClaimedError` → the same "not found" error
  surface as a missing bundle (FR-011a step 6)
- emitting ``download_mode_decision`` BEFORE this helper (the policy
  decides, not us)
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import jwt as _jwt

from cassetta.auth import jwt_tokens
from cassetta.claims import ClaimRecord
from cassetta.config import AppConfig
from cassetta.envelopes import (
    ReferenceEnvelope,
    build_reference_envelope_with_path,
)
from cassetta.protocols.claim_storage import ClaimStorage
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import LimitsPolicy, PolicyContext
from cassetta.protocols.reference_transport import ReferenceTransport
from cassetta.structured_log import struct_log

logger = logging.getLogger("cassetta")


def _download_ttl(policy: LimitsPolicy, identity: Identity) -> int:
    ttls = policy.ttls(PolicyContext(identity=identity))
    return int(ttls["download_claim_ttl"])


def _build_claims(
    bundle_path: str,
    meta: dict[str, Any],
    recipient: str,
    ttl_s: int,
    *,
    iat: int | None = None,
) -> dict[str, Any]:
    now = iat if iat is not None else int(time.time())
    file_names = [str(f["name"]) for f in meta.get("files", [])]
    return {
        "bundle_path": bundle_path,
        "bundle_id": str(meta.get("bundle_id", "")),
        "recipient": recipient,
        "file_names": file_names,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_s,
    }


def _extract_jti(token: str) -> str:
    """Decode a freshly-signed JWT just for its ``jti`` claim."""
    decoded = _jwt.decode(token, options={"verify_signature": False})
    return str(decoded.get("jti") or "")


def _expires_iso(now_epoch: int, ttl_s: int) -> str:
    return datetime.fromtimestamp(
        now_epoch + ttl_s, UTC,
    ).isoformat().replace("+00:00", "Z")


async def build_reference_payload_for_inbox(
    *,
    config: AppConfig,
    policy: LimitsPolicy,
    transport: ReferenceTransport,
    claim_store: ClaimStorage,
    identity: Identity,
    bundle_path: str,
    meta: dict[str, Any],
    recipient: str,
    write_claim: bool,
) -> ReferenceEnvelope:
    """Mint download JWT, (optionally) persist claim sidecar, build envelope.

    When ``write_claim`` is True (pick path), the claim sidecar MUST be
    durably on disk before the credential is returned (FR-011a). When
    False (REST inbox GET), no sidecar is written — the credential's
    ``exp`` is the only time bound.

    Raises :class:`cassetta.claims.BundleClaimedError` when
    ``write_claim=True`` and a sidecar already exists for the freshly-
    minted ``jti`` (vanishingly unlikely — UUIDv4). Callers MUST map
    this to the SAME "bundle not found" surface a late-comer would see.
    """
    ttl_s = _download_ttl(policy, identity)
    now_epoch = int(time.time())
    claims = _build_claims(
        bundle_path, meta, recipient, ttl_s, iat=now_epoch,
    )
    token = jwt_tokens.sign(claims, key=config.jwt_primary_key)
    jti = _extract_jti(token)

    if write_claim:
        created_at = datetime.now(UTC).isoformat()
        record = ClaimRecord(
            schema_version=1,
            jti=jti,
            bundle_path=bundle_path,
            bundle_id=str(meta.get("bundle_id", "")),
            recipient=recipient,
            created_at=created_at,
            files_fetched=[],
        )
        # MUST be fsync'd BEFORE we return `token`. BundleClaimedError
        # propagates to the caller which translates it to "not found".
        await claim_store.issue(record)
        struct_log(
            logger, logging.INFO, "download_claim_issued",
            identity_label=identity.label,
            detail={
                "bundle_id": str(meta.get("bundle_id", "")),
                "jti": jti,
                "files": [f["name"] for f in meta.get("files", [])],
                "exp": now_epoch + ttl_s,
                "recipient": recipient,
                "namespace": "inbox",
            },
        )

    expires_at = _expires_iso(now_epoch, ttl_s)
    return build_reference_envelope_with_path(
        meta=meta, bundle_path=bundle_path, transport=transport,
        token=token, expires_at=expires_at,
    )


def build_reference_payload_for_store(
    *,
    config: AppConfig,
    policy: LimitsPolicy,
    transport: ReferenceTransport,
    identity: Identity,
    bundle_path: str,
    meta: dict[str, Any],
    recipient: str,
) -> ReferenceEnvelope:
    """Mint download JWT + build envelope. No claim sidecar (FR-010a)."""
    ttl_s = _download_ttl(policy, identity)
    now_epoch = int(time.time())
    claims = _build_claims(
        bundle_path, meta, recipient, ttl_s, iat=now_epoch,
    )
    token = jwt_tokens.sign(claims, key=config.jwt_primary_key)
    jti = _extract_jti(token)

    struct_log(
        logger, logging.INFO, "download_claim_issued",
        identity_label=identity.label,
        detail={
            "bundle_id": str(meta.get("bundle_id", "")),
            "jti": jti,
            "files": [f["name"] for f in meta.get("files", [])],
            "exp": now_epoch + ttl_s,
            "recipient": recipient,
            "namespace": "store",
        },
    )

    expires_at = _expires_iso(now_epoch, ttl_s)
    return build_reference_envelope_with_path(
        meta=meta, bundle_path=bundle_path, transport=transport,
        token=token, expires_at=expires_at,
    )
