"""REST endpoint ``POST /upload/{bundle_path:path}`` (Brief 514).

Streaming tar/gzip batch upload. The body is a tar archive (optionally
gzipped) authenticated by a Bearer JWT whose ``bundle_path`` claim MUST
equal the URL-decoded route parameter. The archive is parsed in pipe
mode via stdlib ``tarfile`` inside a threadpool, each entry validated
against the JWT's manifest and streamed into per-file storage;
``meta.json`` is committed last.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import tarfile
import urllib.parse
from datetime import UTC, datetime
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from cassetta.auth import jwt_tokens
from cassetta.auth.jwt_hot_reload import _resolve_secondary
from cassetta.config import AppConfig
from cassetta.defaults.default_limits import (
    DEFAULT_PER_FILE_MAX,
    LimitsRejection,
)
from cassetta.protocols.metrics import MetricsProvider
from cassetta.protocols.storage import BundlePathConflictError, StorageBackend
from cassetta.streaming import SyncStreamReader
from cassetta.structured_log import safe_emit, struct_log

logger = logging.getLogger("cassetta")

router = APIRouter()


def _get_backend(request: Request) -> StorageBackend:
    return request.app.state.backends.backend  # type: ignore[no-any-return]


def _get_config(request: Request) -> AppConfig:
    return request.app.state.config  # type: ignore[no-any-return]


def _get_metrics(request: Request) -> MetricsProvider:
    return request.app.state.backends.metrics_provider  # type: ignore[no-any-return]


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthenticated", "reason": "missing_bearer"},
        )
    return auth.split(" ", 1)[1].strip()


def _reject_unauth(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "unauthenticated", "reason": reason},
    )


def _map_jwt_error(exc: jwt_tokens.TokenError) -> HTTPException:
    if isinstance(exc, jwt_tokens.TokenExpired):
        return _reject_unauth("expired")
    if isinstance(exc, jwt_tokens.TokenNotYetValid):
        return _reject_unauth("immature")
    if isinstance(exc, jwt_tokens.TokenInvalidSignature):
        return _reject_unauth("bad_signature")
    if isinstance(exc, jwt_tokens.TokenMissingClaim):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "unauthenticated",
                "reason": "missing_claim",
                "claim": exc.claim,
            },
        )
    if isinstance(exc, jwt_tokens.RevokedTokenError):
        return _reject_unauth("revoked")
    return _reject_unauth("invalid")


class _ManifestViolation(Exception):
    def __init__(self, reason: str, name: str) -> None:
        super().__init__(f"manifest_violation: reason={reason}, name={name!r}")
        self.reason = reason
        self.name = name


def _stream_tar_into_writer(
    reader: SyncStreamReader,
    writer: Any,
    manifest_files: list[dict[str, Any]],
    tar_mode: str,
    *,
    config: AppConfig,
) -> int:
    """Iterate the tar stream synchronously, writing each entry.

    Runs inside a thread (via :func:`anyio.to_thread.run_sync`). Uses
    :func:`asyncio.run_coroutine_threadsafe` to drive the backend's async
    writer from sync code.
    """
    by_name = {f["name"]: f for f in manifest_files}
    seen: set[str] = set()
    bytes_total = 0
    # Brief 531 FR-020 / FR-025: when the operator hasn't pinned a
    # value, fall back to the 100 MiB ceiling. Read once outside the
    # loop so the comparison stays predictable.
    effective_per_file_max: int = (
        config.limits.per_file_max
        if config.limits.per_file_max is not None
        else DEFAULT_PER_FILE_MAX
    )

    # Get the running loop from the MAIN thread (outside this sync body).
    # We'll pass it in via closure from the async caller.
    loop: asyncio.AbstractEventLoop = _stream_tar_into_writer._loop  # type: ignore[attr-defined]

    with tarfile.open(fileobj=reader, mode=tar_mode) as tf:  # type: ignore[call-overload]
        for info in tf:
            if not info.isreg():
                raise _ManifestViolation("non_regular_entry", info.name)
            name = info.name
            if name not in by_name:
                raise _ManifestViolation("extra_file", name)
            declared_size = int(by_name[name].get("size", 0))
            if info.size != declared_size:
                raise _ManifestViolation("wrong_size", name)
            # Brief 531 FR-021: refuse oversize entries BEFORE reading
            # the body. Insertion is one tarfile call earlier than the
            # spec's "before src.read()" requirement — read avoidance
            # is one extractfile call earlier than that.
            if info.size > effective_per_file_max:
                raise LimitsRejection(
                    error="cap_exceeded",
                    constraint="per_file_max",
                    limit=effective_per_file_max,
                    observed=info.size,
                    reason=(
                        f"per_file_max: max_bytes={effective_per_file_max} "
                        f"actual_bytes={info.size}"
                    ),
                )
            src = tf.extractfile(info)
            if src is None:
                raise _ManifestViolation("non_regular_entry", name)
            # Read bytes fully from the tarfile member (it's in-memory at
            # this point since we already hold the tar info). For very large
            # files we could chunk, but tarfile.extractfile gives us a
            # tarfile.ExFileObject that lets us read sequentially.
            data = src.read()
            if len(data) != declared_size:
                raise _ManifestViolation("wrong_size", name)
            fut = asyncio.run_coroutine_threadsafe(
                writer.write_file(name, io.BytesIO(data)), loop,
            )
            fut.result()
            seen.add(name)
            bytes_total += len(data)

    missing = set(by_name) - seen
    if missing:
        first = sorted(missing)[0]
        raise _ManifestViolation("missing_file", first)
    return bytes_total


async def _pump_stream(request: Request, reader: SyncStreamReader) -> None:
    """Copy the async body into the reader and signal EOF."""
    try:
        async for chunk in request.stream():
            if chunk:
                reader.feed(chunk)
    finally:
        reader.close()


@router.post("/upload/{bundle_path:path}")
async def upload_bundle(
    bundle_path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
) -> JSONResponse:
    config = _get_config(request)
    metrics = _get_metrics(request)

    token = _extract_bearer(request)
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
        raise _map_jwt_error(exc) from exc

    if claims.get("mode") != "batch":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "unauthenticated", "reason": "wrong_mode"},
        )

    # Re-assemble the bundle path from the raw URL to preserve the exact
    # encoding used at sign time. FastAPI decodes ``%2F`` → ``/`` so the
    # captured ``bundle_path`` already matches the JWT claim format.
    claim_bundle_path = str(claims.get("bundle_path", ""))
    decoded_param = urllib.parse.unquote(bundle_path)
    if decoded_param != claim_bundle_path:
        raise _reject_unauth("bundle_path_mismatch")

    manifest_raw = claims.get("manifest") or {}
    manifest_files: list[dict[str, Any]] = list(manifest_raw.get("files", []))
    bundle_id = str(claims["bundle_id"])
    sender = claims.get("sender")

    content_encoding = request.headers.get("content-encoding", "").lower()
    tar_mode = "r|gz" if content_encoding == "gzip" else "r|"

    struct_log(
        logger, logging.INFO, "upload_stream_start",
        detail={
            "bundle_id": bundle_id,
            "mode": "batch",
            "content_encoding": content_encoding or None,
        },
    )

    try:
        writer = await backend.open_bundle_write(claim_bundle_path)
    except BundlePathConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "bundle_path_conflict",
                "kind": exc.kind,
            },
        ) from exc
    except FileExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "bundle_path_conflict", "kind": "occupied"},
        ) from exc

    reader = SyncStreamReader()
    loop = asyncio.get_running_loop()
    _stream_tar_into_writer._loop = loop  # type: ignore[attr-defined]

    pump_task = asyncio.create_task(_pump_stream(request, reader))
    try:
        bytes_total = await anyio.to_thread.run_sync(
            functools.partial(
                _stream_tar_into_writer,
                reader, writer, manifest_files, tar_mode,
                config=config,
            ),
        )
        await pump_task
    except _ManifestViolation as exc:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass
        await writer.abort()
        struct_log(
            logger, logging.WARNING, "upload_manifest_violation",
            detail={
                "bundle_id": bundle_id,
                "violation": exc.reason,
                "name": exc.name,
            },
        )
        struct_log(
            logger, logging.WARNING, "upload_rollback",
            detail={"bundle_id": bundle_id, "reason": exc.reason},
        )
        # Brief 533 FR-002 + FR-004: rejected + manifest_violations{reason}.
        safe_emit(
            metric_name="cassetta.upload.operations",
            metric_tags={"result": "rejected"},
            metrics=metrics,
        )
        safe_emit(
            metric_name="cassetta.upload.manifest_violations",
            metric_tags={"reason": exc.reason},
            metrics=metrics,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "manifest_violation",
                "reason": exc.reason,
                "name": exc.name,
            },
        ) from exc
    except tarfile.TarError as exc:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass
        await writer.abort()
        struct_log(
            logger, logging.WARNING, "upload_rollback",
            detail={"bundle_id": bundle_id, "reason": "tar_parse_error"},
        )
        # Brief 533 FR-002 / SC-022: mid-stream archive failure → result=error.
        safe_emit(
            metric_name="cassetta.upload.operations",
            metric_tags={"result": "error"},
            metrics=metrics,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "tar_parse_error", "reason": str(exc)},
        ) from exc
    except LimitsRejection:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass
        await writer.abort()
        struct_log(
            logger, logging.WARNING, "upload_rollback",
            detail={"bundle_id": bundle_id, "reason": "limits_rejection"},
        )
        # Brief 533 SC-022: per-file-max from brief 531 firing mid-stream
        # AFTER manifest validation → result=error, no upload.bytes.
        safe_emit(
            metric_name="cassetta.upload.operations",
            metric_tags={"result": "error"},
            metrics=metrics,
        )
        raise
    except Exception:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass
        await writer.abort()
        struct_log(
            logger, logging.WARNING, "upload_rollback",
            detail={"bundle_id": bundle_id, "reason": "exception"},
        )
        # Brief 533: any other mid-stream failure → result=error.
        safe_emit(
            metric_name="cassetta.upload.operations",
            metric_tags={"result": "error"},
            metrics=metrics,
        )
        raise

    meta: dict[str, Any] = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "sender": sender,
        "created_at": datetime.now(UTC).isoformat(),
        "content_type": "bundle" if len(manifest_files) > 1 else "file",
        "file_count": len(manifest_files),
        "files": manifest_files,
    }
    try:
        await writer.commit(meta)
    except Exception:
        await writer.abort()
        struct_log(
            logger, logging.WARNING, "upload_rollback",
            detail={"bundle_id": bundle_id, "reason": "commit_failed"},
        )
        # Brief 533: commit failure → result=error.
        safe_emit(
            metric_name="cassetta.upload.operations",
            metric_tags={"result": "error"},
            metrics=metrics,
        )
        raise

    struct_log(
        logger, logging.INFO, "upload_stream_complete",
        detail={"bundle_id": bundle_id, "bytes_transferred": bytes_total},
    )
    # Brief 533 FR-002 + FR-003: success → ok + upload.bytes (untagged).
    safe_emit(
        metric_name="cassetta.upload.operations",
        metric_tags={"result": "ok"},
        metrics=metrics,
    )
    safe_emit(
        metric_name="cassetta.upload.bytes",
        metric_value=bytes_total,
        metrics=metrics,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"bundle_id": bundle_id, "ok": True},
    )
