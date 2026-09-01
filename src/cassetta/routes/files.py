"""REST endpoints for the store namespace (GET/PUT/DELETE /files/...)."""

import io
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from cassetta.auth import get_current_identity
from cassetta.config import AppConfig
from cassetta.defaults.default_limits import LimitsRejection, _format_reason
from cassetta.dependencies import get_limits_policy, get_metrics
from cassetta.downloads import build_reference_payload_for_store
from cassetta.envelopes import build_inline_envelope
from cassetta.mime import pick_mime
from cassetta.models import (
    DeletedResponse,
    FileInfo,
    FileListResponse,
    FileMetadata,
    FileRecord,
)
from cassetta.path_validation import PathValidationError, validate_path
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import (
    DownloadEntry,
    LimitsPolicy,
    ManifestFile,
    PolicyContext,
    UploadManifest,
)
from cassetta.protocols.metrics import MetricsProvider
from cassetta.protocols.reference_transport import ReferenceTransport
from cassetta.protocols.storage import BundlePathConflictError, StorageBackend
from cassetta.structured_log import safe_emit, struct_log

logger = logging.getLogger("cassetta")

router = APIRouter()

RESERVED_PREFIXES = ("inbox/", "store/")
STORE_NAMESPACE = "store"
SCHEMA_VERSION = 1


def _get_backend(request: Request) -> StorageBackend:
    return request.app.state.backends.backend  # type: ignore[no-any-return]


def _get_config(request: Request) -> AppConfig:
    return request.app.state.config  # type: ignore[no-any-return]


def _get_policy(request: Request) -> AccessPolicy:
    return request.app.state.backends.access_policy  # type: ignore[no-any-return]


def _get_transport(request: Request) -> ReferenceTransport:
    return request.app.state.backends.reference_transport  # type: ignore[no-any-return]


def _policy_kind_fields(policy: AccessPolicy) -> tuple[str, str]:
    """Brief 533 FR-022 — see ``routes/inbox.py:_policy_kind_fields``."""
    kind = getattr(policy, "kind", "core")
    field = kind if kind in ("core", "cloud") else "core"
    tag = "team" if field == "cloud" else "core"
    return field, tag


async def _enforce(
    request: Request,
    identity: Identity,
    resource: str,
    action: str,
    metrics: MetricsProvider | None = None,
) -> None:
    policy = _get_policy(request)
    if not await policy.check(identity, resource, action):
        # Brief 533 FR-007a / FR-022 / FR-063: paired event + counter via
        # safe_emit; policy_kind derived from policy.kind.
        field_kind, tag_kind = _policy_kind_fields(policy)
        safe_emit(
            logger,
            logging.INFO,
            "policy.denied",
            identity_label=identity.label,
            identity_extra=identity.extra or None,
            resource=resource,
            action=action,
            result="denied",
            detail={"policy_kind": field_kind},
            metric_name="cassetta.policy.decisions",
            metric_tags={"result": "denied", "policy_kind": tag_kind},
            metrics=metrics,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _is_expired_by_meta(created_at_iso: str, ttl: int) -> bool:
    if ttl <= 0:
        return False
    created = datetime.fromisoformat(created_at_iso)
    return time.time() - created.timestamp() > ttl


def _compute_remaining_ttl(created_at_iso: str, ttl: int) -> int | None:
    if ttl <= 0:
        return None
    created = datetime.fromisoformat(created_at_iso)
    elapsed = time.time() - created.timestamp()
    remaining = int(ttl - elapsed)
    return max(remaining, 0)


async def _evaluate_or_raise(
    policy: LimitsPolicy,
    identity: Identity,
    file_parts: list[tuple[str, bytes, str | None]],
) -> None:
    entries: list[ManifestFile] = [
        {"name": name, "size": len(data), "mime": explicit} for name, data, explicit in file_parts
    ]
    manifest: UploadManifest = {"file_count": len(entries), "files": entries}
    ctx = PolicyContext(identity=identity)
    decision = await policy.evaluate_upload(ctx, manifest)
    if "error" in decision:
        constraint = decision["constraint"]
        file_name = None
        if constraint == "per_file_max":
            for f in entries:
                if f["size"] == decision["observed"]:
                    file_name = f"file {f['name']!r}"
                    break
        reason = _format_reason(
            decision["error"],
            constraint,
            decision.get("limit"),
            decision["observed"],
            context=file_name,
        )
        struct_log(
            logger,
            logging.INFO,
            "policy.rejection",
            identity_label=identity.label,
            detail={
                "error": decision["error"],
                "constraint": constraint,
                "limit": decision.get("limit"),
                "observed": decision["observed"],
            },
        )
        raise LimitsRejection(
            error=decision["error"],
            constraint=constraint,
            limit=decision.get("limit"),
            observed=decision["observed"],
            reason=reason,
        )
    if decision["mode"] != "inline":
        total_size = sum(f["size"] for f in entries)
        inline_cap = policy.advertise_limits(ctx).get("max_inline_size")
        reason = _format_reason(
            "batch_required",
            "max_inline_size",
            inline_cap,
            total_size,
        )
        struct_log(
            logger,
            logging.INFO,
            "policy.rejection",
            identity_label=identity.label,
            detail={
                "error": "batch_required",
                "constraint": "max_inline_size",
                "limit": inline_cap,
                "observed": total_size,
            },
        )
        raise LimitsRejection(
            error="batch_required",
            constraint="max_inline_size",
            limit=inline_cap,
            observed=total_size,
            reason=reason,
        )
    struct_log(
        logger,
        logging.DEBUG,
        "policy.upload_decision",
        identity_label=identity.label,
        detail={
            "file_count": len(entries),
            "total_size": sum(f["size"] for f in entries),
            "decision": "inline",
        },
    )


async def _collect_bundle_files(
    backend: StorageBackend, bundle_path: str, records: list[dict[str, Any]]
) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for record in records:
        handle = await backend.open_bundle_file_read(bundle_path, record["name"])
        try:
            data = handle.read()
        finally:
            handle.close()
        out.append((record["name"], data))
    return out


async def _write_store_bundle(
    backend: StorageBackend,
    bundle_path: str,
    files: list[tuple[str, bytes, str | None]],
    *,
    bundle_id: str,
) -> dict[str, Any]:
    writer = await backend.open_bundle_write(bundle_path)
    try:
        records: list[dict[str, Any]] = []
        for name, data, explicit_mime in files:
            await writer.write_file(name, io.BytesIO(data))
            records.append(
                {
                    "name": name,
                    "size": len(data),
                    "mime": pick_mime(name, explicit=explicit_mime),
                }
            )
        meta: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "bundle_id": bundle_id,
            "sender": None,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": len(records),
            "files": records,
        }
        await writer.commit(meta)
        return meta
    except Exception:
        await writer.abort()
        raise


@router.put("/files/{path:path}", status_code=201)
async def upload_file(
    path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
    limits_policy: Annotated[LimitsPolicy, Depends(get_limits_policy)],
) -> FileMetadata:
    try:
        config = _get_config(request)
        validate_path(path, allowed_chars=config.allowed_path_chars)
    except PathValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    for prefix in RESERVED_PREFIXES:
        if path.startswith(prefix):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Reserved path prefix: {prefix}",
            )

    await _enforce(request, identity, f"files:{path}", "write", metrics)

    content_type_header = request.headers.get("content-type", "")
    is_multipart = "multipart/form-data" in content_type_header

    if is_multipart:
        form = await request.form()
        file_parts: list[tuple[str, bytes, str | None]] = []
        for _key, upload in form.multi_items():
            if isinstance(upload, UploadFile) and upload.filename:
                file_content = await upload.read()
                explicit = upload.content_type if upload.content_type else None
                file_parts.append((upload.filename, file_content, explicit))
        if not file_parts:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No files found in multipart request",
            )
        total_content_size = sum(len(c) for _, c, _ in file_parts)
    else:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
        content_bytes = bytes(body)
        file_parts = [(path.split("/")[-1], content_bytes, None)]
        total_content_size = len(content_bytes)

    await _evaluate_or_raise(limits_policy, identity, file_parts)

    bundle_path = f"{STORE_NAMESPACE}/{path}"
    # Treat an existing committed bundle at the same path as an overwrite.
    existed = False
    try:
        await backend.read_bundle_meta(bundle_path)
        existed = True
    except FileNotFoundError:
        pass
    if existed:
        try:
            await backend.delete_bundle(bundle_path)
        except FileNotFoundError:
            pass

    bundle_id = uuid.uuid4().hex
    try:
        await _write_store_bundle(
            backend,
            bundle_path,
            file_parts,
            bundle_id=bundle_id,
        )
    except BundlePathConflictError as exc:
        return JSONResponse(  # type: ignore[return-value]
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "bundle_path_conflict",
                "conflicting_path": exc.conflicting_path,
                "kind": exc.kind,
            },
        )

    safe_emit(
        logger,
        logging.INFO,
        "file.uploaded",
        identity_label=identity.label,
        identity_extra=identity.extra or None,
        resource=f"files:{path}",
        action="put",
        result="ok",
        detail={"size": total_content_size, "bundle_id": bundle_id},
        metric_name="cassetta.files.operations",
        metric_tags={"action": "put"},
        metrics=metrics,
    )
    safe_emit(
        metric_name="cassetta.files.bytes",
        metric_value=total_content_size,
        metric_tags={"action": "put"},
        metrics=metrics,
    )

    response = FileMetadata(path=path, size=total_content_size)
    if existed:
        return Response(  # type: ignore[return-value]
            content=response.model_dump_json(),
            media_type="application/json",
            status_code=200,
        )
    return response


@router.get("/files/")
async def list_files(
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
    prefix: str = "",
) -> FileListResponse:
    config = _get_config(request)
    await _enforce(request, identity, "files:*", "list", metrics)

    scan_prefix = f"{STORE_NAMESPACE}/" + (prefix.lstrip("/") if prefix else "")
    entries: list[FileInfo] = []
    for ref in backend.list_bundles(scan_prefix):
        try:
            meta = await backend.read_bundle_meta(ref.path)
        except FileNotFoundError:
            struct_log(
                logger,
                logging.WARNING,
                "files.foreign_object",
                resource="files:*",
                detail={"path": ref.path},
            )
            continue
        created_at_iso = str(meta.get("created_at", ""))
        if _is_expired_by_meta(created_at_iso, config.default_ttl):
            continue

        created_dt = datetime.fromisoformat(created_at_iso)
        files_entries = [FileRecord(name=f["name"], size=int(f["size"]), mime=f["mime"]) for f in meta.get("files", [])]
        total_size = sum(f.size for f in files_entries)
        remaining = _compute_remaining_ttl(created_at_iso, config.default_ttl)
        display_path = ref.path.removeprefix(f"{STORE_NAMESPACE}/")
        entries.append(
            FileInfo(
                path=display_path,
                size=total_size,
                created_at=created_dt,
                remaining_ttl=remaining,
                file_count=int(meta.get("file_count", len(files_entries))),
                bundle_id=str(meta.get("bundle_id", "")),
                files=files_entries,
            )
        )

    safe_emit(
        logger,
        logging.INFO,
        "file.listed",
        identity_label=identity.label,
        identity_extra=identity.extra or None,
        resource=f"files:{prefix}*",
        action="list",
        result="ok",
        detail={"count": len(entries)},
        metric_name="cassetta.files.operations",
        metric_tags={"action": "list"},
        metrics=metrics,
    )

    return FileListResponse(files=entries)


@router.get("/files/{path:path}/peek")
async def peek_store_file(
    path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> JSONResponse:
    try:
        validate_path(path)
    except PathValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    await _enforce(request, identity, f"files:{path}", "peek", metrics)

    config = _get_config(request)
    bundle_path = f"{STORE_NAMESPACE}/{path}"
    try:
        meta = await backend.read_bundle_meta(bundle_path)
    except FileNotFoundError as exc:
        struct_log(
            logger,
            logging.INFO,
            "peek.not_found",
            identity_label=identity.label,
            resource=f"files:{path}",
            detail={"path": path, "found": False, "reason": "absent"},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        ) from exc

    if _is_expired_by_meta(str(meta.get("created_at", "")), config.default_ttl):
        struct_log(
            logger,
            logging.INFO,
            "peek.not_found",
            identity_label=identity.label,
            resource=f"files:{path}",
            detail={"path": path, "found": False, "reason": "expired"},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )

    file_count = int(meta.get("file_count", 0))
    total_size = sum(int(f.get("size", 0)) for f in meta.get("files", []))
    safe_emit(
        logger,
        logging.DEBUG,
        "peek.ok",
        identity_label=identity.label,
        resource=f"files:{path}",
        detail={
            "path": path,
            "found": True,
            "file_count": file_count,
            "total_size": total_size,
            "bundle_id": meta.get("bundle_id"),
        },
        metric_name="cassetta.files.operations",
        metric_tags={"action": "peek"},
        metrics=metrics,
    )
    return JSONResponse(
        content={"bundle": meta},
        headers={"X-Cassetta-File-Count": str(file_count)},
    )


@router.get("/files/{path:path}")
async def download_file(
    path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
    limits_policy: Annotated[LimitsPolicy, Depends(get_limits_policy)],
) -> Response:
    try:
        validate_path(path)
    except PathValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    await _enforce(request, identity, f"files:{path}", "read", metrics)

    config = _get_config(request)
    bundle_path = f"{STORE_NAMESPACE}/{path}"
    try:
        meta = await backend.read_bundle_meta(bundle_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        ) from exc

    if _is_expired_by_meta(str(meta.get("created_at", "")), config.default_ttl):
        try:
            await backend.delete_bundle(bundle_path)
        except FileNotFoundError:
            pass
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )

    file_count = int(meta.get("file_count", 1))
    records = list(meta.get("files", []))

    total_size = sum(int(f.get("size", 0)) for f in records)
    entry: DownloadEntry = {"file_count": file_count, "total_size": total_size}
    decision = await limits_policy.evaluate_download(
        PolicyContext(identity=identity),
        entry,
    )
    mode = decision.get("mode")
    struct_log(
        logger,
        logging.DEBUG,
        "download_mode_decision",
        identity_label=identity.label,
        detail={"bundle_id": meta.get("bundle_id"), "mode": mode, "namespace": "store"},
    )

    safe_emit(
        logger,
        logging.INFO,
        "file.downloaded",
        identity_label=identity.label,
        identity_extra=identity.extra or None,
        resource=f"files:{path}",
        action="get",
        result="ok",
        detail={"file_count": file_count, "bundle_id": meta.get("bundle_id")},
        metric_name="cassetta.files.operations",
        metric_tags={"action": "get"},
        metrics=metrics,
    )

    if mode == "reference":
        transport = _get_transport(request)
        ref_envelope = build_reference_payload_for_store(
            config=config,
            policy=limits_policy,
            transport=transport,
            identity=identity,
            bundle_path=bundle_path,
            meta=meta,
            recipient=identity.label,
        )
        safe_emit(
            metric_name="cassetta.files.bytes",
            metric_value=total_size,
            metric_tags={"action": "get"},
            metrics=metrics,
        )
        return JSONResponse(
            content=dict(ref_envelope),
            headers={"X-Cassetta-File-Count": str(file_count)},
        )

    payloads = await _collect_bundle_files(backend, bundle_path, records)
    inline_size = sum(len(d) for _, d in payloads)
    safe_emit(
        metric_name="cassetta.files.bytes",
        metric_value=inline_size,
        metric_tags={"action": "get"},
        metrics=metrics,
    )
    inline_envelope = build_inline_envelope(meta, payloads)
    return JSONResponse(
        content=dict(inline_envelope),
        headers={"X-Cassetta-File-Count": str(file_count)},
    )


@router.delete("/files/{path:path}")
async def delete_file(
    path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> DeletedResponse:
    try:
        validate_path(path)
    except PathValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    await _enforce(request, identity, f"files:{path}", "delete", metrics)

    bundle_path = f"{STORE_NAMESPACE}/{path}"
    try:
        await backend.delete_bundle(bundle_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        ) from exc

    safe_emit(
        logger,
        logging.INFO,
        "file.deleted",
        identity_label=identity.label,
        identity_extra=identity.extra or None,
        resource=f"files:{path}",
        action="delete",
        result="ok",
        metric_name="cassetta.files.operations",
        metric_tags={"action": "delete"},
        metrics=metrics,
    )

    return DeletedResponse(deleted=path)
