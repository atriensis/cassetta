"""REST endpoint ``GET /download/{bundle_path:path}/{name}`` (Brief 515).

Streams one file from a claimed bundle, authorized by a download JWT
plus a matching identity header. Two-factor contract per FR-006a:
Authorization: Bearer <download_jwt> — signature + exp + bundle_path +
name-in-claim; AND X-Sender: <label> — identity self-declaration
compared to the JWT's ``recipient`` claim.

The bundle's claim sidecar (inbox only) is updated after the last byte
ships to the client; when ``files_fetched`` covers the whole manifest,
the bundle is deleted and the claim is dropped (FR-009 + FR-015).
"""

from __future__ import annotations

import logging
import urllib.parse
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from cassetta.auth import jwt_tokens
from cassetta.auth.jwt_hot_reload import _resolve_secondary
from cassetta.auth.observability import Reason, emit_auth_failure
from cassetta.protocols.claim_storage import ClaimStorage
from cassetta.protocols.metrics import MetricsProvider
from cassetta.protocols.storage import StorageBackend
from cassetta.structured_log import safe_emit, struct_log

logger = logging.getLogger("cassetta")

router = APIRouter()

_INBOX_PREFIX = "inbox/"

# WWW-Authenticate header emitted on every 401 per RFC 6750 + FR-006.
_WWW_AUTHENTICATE = 'Bearer error="invalid_token"'


class DownloadError(Exception):
    """Custom abort signal for the download endpoint.

    Carries a body matching ``contracts/download-endpoint.md`` verbatim
    (``{"error": ..., "reason": ...}``) — NOT wrapped in FastAPI's
    default ``{"detail": ...}`` envelope. An app-level exception handler
    registered in :mod:`cassetta.app` converts these to ``JSONResponse``.
    """

    def __init__(
        self,
        status_code: int,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"download_error:{status_code}:{body}")
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}


def _get_backend(request: Request) -> StorageBackend:
    return request.app.state.backends.backend  # type: ignore[no-any-return]


def _get_claim_store(request: Request) -> ClaimStorage:
    return request.app.state.backends.claim_store  # type: ignore[no-any-return]


def _get_metrics(request: Request) -> MetricsProvider:
    return request.app.state.backends.metrics_provider  # type: ignore[no-any-return]


def _unauth(reason: str, extra: dict[str, Any] | None = None) -> DownloadError:
    body: dict[str, Any] = {"error": "unauthenticated", "reason": reason}
    if extra:
        body.update(extra)
    return DownloadError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        body=body,
        headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
    )


def _forbidden(reason: str, extra: dict[str, Any] | None = None) -> DownloadError:
    body: dict[str, Any] = {"error": "forbidden", "reason": reason}
    if extra:
        body.update(extra)
    return DownloadError(
        status_code=status.HTTP_403_FORBIDDEN,
        body=body,
    )


def _not_found(reason: str) -> DownloadError:
    return DownloadError(
        status_code=status.HTTP_404_NOT_FOUND,
        body={"error": "not_found", "reason": reason},
    )


def _map_jwt_error(exc: jwt_tokens.TokenError) -> DownloadError:
    if isinstance(exc, jwt_tokens.TokenExpired):
        return _unauth("expired")
    if isinstance(exc, jwt_tokens.TokenNotYetValid):
        return _unauth("immature")
    if isinstance(exc, jwt_tokens.TokenInvalidSignature):
        return _unauth("bad_signature")
    if isinstance(exc, jwt_tokens.TokenMissingClaim):
        return _unauth("missing_claim", {"claim": exc.claim})
    if isinstance(exc, jwt_tokens.RevokedTokenError):
        return _unauth("revoked")
    return _unauth("invalid")


def _jwt_error_to_reason(exc: jwt_tokens.TokenError) -> Reason:
    """Map a TokenError subclass to the auth.failure reason (Brief 529 R4)."""
    if isinstance(exc, jwt_tokens.TokenExpired):
        return "jwt_expired"
    return "jwt_invalid"


async def download_error_handler(
    _request: Request,
    exc: DownloadError,
) -> JSONResponse:
    """App-level handler registered in :mod:`cassetta.app`."""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.body,
        headers=exc.headers,
    )


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise _unauth("missing_bearer")
    return auth.split(" ", 1)[1].strip()


def _resolve_identity_label(request: Request) -> str | None:
    """Resolve the caller's declared identity label.

    Dev mode: skip the check entirely by returning "*" (sentinel matched
    against any recipient). Auth mode: read ``X-Sender``; if absent,
    return ``None`` and let the handler raise 401 per the FR-006a
    two-factor contract.
    """
    if getattr(request.app.state, "dev_mode", False):
        return "*"
    sender = request.headers.get("x-sender")
    if sender:
        return sender
    return None


def _split_raw_path(request: Request) -> tuple[str, str]:
    """Parse bundle_path + name from ``scope['raw_path']``.

    The URL shape ``/download/{bundle_path_urlencoded}/{name_urlencoded}``
    depends on the literal `/` between the two percent-encoded segments
    NOT being collapsed by ASGI's path-decoding step. ``raw_path`` on
    uvicorn/Starlette preserves percent-encoding, so splitting at the
    last raw `/` correctly separates the two parts.

    Returns the URL-decoded ``(bundle_path, name)`` tuple.
    """
    raw = request.scope.get("raw_path")
    if raw is None:
        raw = request.url.path.encode("utf-8")
    if isinstance(raw, bytes):
        raw_str = raw.decode("utf-8")
    else:
        raw_str = str(raw)
    # Strip query string if present.
    if "?" in raw_str:
        raw_str = raw_str.split("?", 1)[0]
    if not raw_str.startswith("/download/"):
        raise _not_found("bundle_gone")
    remainder = raw_str[len("/download/") :]
    if "/" not in remainder:
        # Only one segment — no bundle_path/name separator.
        raise _not_found("bundle_gone")
    bundle_path_enc, name_enc = remainder.rsplit("/", 1)
    return urllib.parse.unquote(bundle_path_enc), urllib.parse.unquote(name_enc)


@router.get("/download/{full_path:path}")
async def download_file(
    full_path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
) -> StreamingResponse:
    del full_path  # consumed via raw_path for correct %2F splitting
    claim_store = _get_claim_store(request)
    metrics = _get_metrics(request)

    # Step 1: extract Bearer.
    token = _extract_bearer(request)

    # Step 2: JWT verify (signature, exp, nbf, required claims).
    # Brief 531: read the runtime slots so a SIGHUP-driven rotation
    # takes effect on the next request without a restart.
    slots = request.app.state.jwt_keys
    try:
        claims = jwt_tokens.verify(
            token,
            primary=slots.primary,
            secondary=_resolve_secondary(slots),
        )
    except jwt_tokens.TokenError as exc:
        emit_auth_failure(
            metrics=request.app.state.backends.metrics_provider,
            source="download",
            reason=_jwt_error_to_reason(exc),
            identity_hint=None,
        )
        raise _map_jwt_error(exc) from exc

    # Required 515-specific claims.
    for required in ("bundle_path", "bundle_id", "recipient", "file_names"):
        if required not in claims:
            raise _unauth("missing_claim", {"claim": required})

    # Step 3: parse bundle_path + name from raw URL.
    decoded_path, decoded_name = _split_raw_path(request)

    # Step 4: bundle_path claim check.
    claim_bundle_path = str(claims["bundle_path"])
    if decoded_path != claim_bundle_path:
        raise _unauth("bundle_path_mismatch")

    # Step 5: name in JWT's file_names whitelist.
    claim_file_names = [str(n) for n in claims.get("file_names") or []]
    if decoded_name not in claim_file_names:
        raise _unauth("name_not_in_claim")

    # Step 5: identity check (FR-006a defense-in-depth).
    identity_label = _resolve_identity_label(request)
    if identity_label is None:
        # Missing identity header — treat as unauthenticated.
        raise _unauth("identity_missing")
    claim_recipient = str(claims["recipient"])
    if identity_label != "*" and identity_label != claim_recipient:
        struct_log(
            logger,
            logging.WARNING,
            "download_identity_mismatch",
            detail={
                "identity_label": identity_label,
                "claim_recipient": claim_recipient,
                "bundle_path": claim_bundle_path,
            },
        )
        # Brief 533 FR-005 / SC-025: distinct from authentication.
        safe_emit(
            metric_name="cassetta.download.operations",
            metric_tags={"result": "identity_mismatch"},
            metrics=metrics,
        )
        raise _forbidden("identity_mismatch")

    # Step 6: meta + name in manifest.
    try:
        meta = await backend.read_bundle_meta(claim_bundle_path)
    except FileNotFoundError as exc:
        # Brief 533 FR-005 / SC-024: bundle_gone collapses to not_found.
        safe_emit(
            metric_name="cassetta.download.operations",
            metric_tags={"result": "not_found"},
            metrics=metrics,
        )
        raise _not_found("bundle_gone") from exc

    manifest_files = list(meta.get("files", []))
    manifest_entry = next(
        (f for f in manifest_files if str(f.get("name")) == decoded_name),
        None,
    )
    if manifest_entry is None:
        # Brief 533 FR-005 / SC-024: name_not_in_manifest collapses to not_found.
        safe_emit(
            metric_name="cassetta.download.operations",
            metric_tags={"result": "not_found"},
            metrics=metrics,
        )
        raise _not_found("name_not_in_manifest")

    mime = str(manifest_entry.get("mime") or "application/octet-stream")
    size_int = int(manifest_entry.get("size", 0))

    # Step 7: stream. Open handle first, then wrap in StreamingResponse.
    try:
        handle = await backend.open_bundle_file_read(
            claim_bundle_path,
            decoded_name,
        )
    except FileNotFoundError as exc:
        safe_emit(
            metric_name="cassetta.download.operations",
            metric_tags={"result": "not_found"},
            metrics=metrics,
        )
        raise _not_found("bundle_gone") from exc

    jti = str(claims.get("jti") or "")
    bundle_id = str(claims.get("bundle_id") or "")
    is_inbox = claim_bundle_path.startswith(_INBOX_PREFIX)
    response_headers: dict[str, str] = {"Content-Length": str(size_int)}

    body = _body_generator(
        handle=handle,
        claim_store=claim_store,
        backend=backend,
        jti=jti,
        bundle_path=claim_bundle_path,
        bundle_id=bundle_id,
        name=decoded_name,
        manifest_files=manifest_files,
        is_inbox=is_inbox,
        metrics=metrics,
    )
    return StreamingResponse(
        body,
        media_type=mime,
        headers=response_headers,
    )


async def _body_generator(
    *,
    handle: Any,
    claim_store: ClaimStorage,
    backend: StorageBackend,
    jti: str,
    bundle_path: str,
    bundle_id: str,
    name: str,
    manifest_files: list[dict[str, Any]],
    is_inbox: bool,
    metrics: MetricsProvider,
) -> AsyncIterator[bytes]:
    total = 0
    try:
        chunk_size = 64 * 1024
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            yield chunk
    finally:
        handle.close()
    struct_log(
        logger,
        logging.DEBUG,
        "download_file_fetched",
        detail={
            "bundle_id": bundle_id,
            "jti": jti,
            "name": name,
            "bytes_transferred": total,
        },
    )
    # Brief 533 FR-005 / FR-006: success path emits ok + download.bytes
    # AFTER the streamed body completes (SC-023).
    safe_emit(
        metric_name="cassetta.download.operations",
        metric_tags={"result": "ok"},
        metrics=metrics,
    )
    safe_emit(
        metric_name="cassetta.download.bytes",
        metric_value=total,
        metrics=metrics,
    )
    if is_inbox:
        await _post_stream_claim_accounting(
            claim_store=claim_store,
            backend=backend,
            jti=jti,
            bundle_path=bundle_path,
            bundle_id=bundle_id,
            name=name,
            manifest_files=manifest_files,
        )


async def _post_stream_claim_accounting(
    *,
    claim_store: ClaimStorage,
    backend: StorageBackend,
    jti: str,
    bundle_path: str,
    bundle_id: str,
    name: str,
    manifest_files: list[dict[str, Any]],
) -> None:
    """Mark file fetched; on completion, delete the bundle + claim."""
    record = await claim_store.get(jti)
    if record is None:
        # No sidecar (e.g. reaper collected it or the issuer was a REST
        # non-pick read). Nothing to update.
        return
    try:
        updated = await claim_store.mark_fetched(jti, name)
    except FileNotFoundError:
        return

    manifest_names = {str(f.get("name")) for f in manifest_files}
    fetched = set(updated.files_fetched)
    if manifest_names.issubset(fetched):
        # Completion — delete bundle + drop claim.
        try:
            await backend.delete_bundle(bundle_path)
        except FileNotFoundError:
            pass
        await claim_store.delete(jti)
        total_bytes = sum(int(f.get("size", 0)) for f in manifest_files)
        struct_log(
            logger,
            logging.INFO,
            "download_claim_completed",
            detail={
                "bundle_id": bundle_id,
                "jti": jti,
                "total_bytes": total_bytes,
                "namespace": "inbox",
            },
        )
