"""REST endpoints for agent inbox operations."""

import logging
import time
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from cassetta.auth import get_current_identity
from cassetta.claims import BundleClaimedError
from cassetta.config import AppConfig
from cassetta.dependencies import get_metrics
from cassetta.downloads import build_reference_payload_for_inbox
from cassetta.envelopes import build_inline_envelope
from cassetta.models import (
    BundlePathConflict,
    InboxFileInfo,
    InboxListResponse,
    PeekResponse,
)
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.claim_storage import ClaimStorage
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import DownloadEntry, LimitsPolicy, PolicyContext
from cassetta.protocols.metrics import MetricsProvider
from cassetta.protocols.reference_transport import ReferenceTransport
from cassetta.protocols.storage import BundlePathConflictError, StorageBackend
from cassetta.structured_log import safe_emit, struct_log

logger = logging.getLogger("cassetta")

router = APIRouter()

SCHEMA_VERSION = 1
INBOX_NAMESPACE = "inbox"


def _get_backend(request: Request) -> StorageBackend:
    return request.app.state.backends.backend  # type: ignore[no-any-return]


def _get_config(request: Request) -> AppConfig:
    return request.app.state.config  # type: ignore[no-any-return]


def _get_policy(request: Request) -> AccessPolicy:
    return request.app.state.backends.access_policy  # type: ignore[no-any-return]


def _get_limits_policy(request: Request) -> LimitsPolicy:
    return request.app.state.backends.limits_policy  # type: ignore[no-any-return]


def _get_transport(request: Request) -> ReferenceTransport:
    return request.app.state.backends.reference_transport  # type: ignore[no-any-return]


def _get_claim_store(request: Request) -> ClaimStorage:
    return request.app.state.backends.claim_store  # type: ignore[no-any-return]


def _policy_kind_fields(policy: AccessPolicy) -> tuple[str, str]:
    """Brief 533 FR-022 — derive (event_field, counter_tag) from policy.kind.

    Cloud team-policy denials use ``policy_kind=cloud`` on the
    ``policy.denied`` log field AND ``policy_kind=team`` on the
    ``cassetta.policy.decisions`` counter tag.
    """
    kind = getattr(policy, "kind", "core")
    field = kind if kind in ("core", "cloud") else "core"
    tag = "team" if field == "cloud" else "core"
    return field, tag


async def _enforce(
    request: Request, identity: Identity, resource: str, action: str,
    metrics: MetricsProvider | None = None,
) -> None:
    policy = _get_policy(request)
    if not await policy.check(identity, resource, action):
        # Brief 533 FR-007a / FR-022 / FR-063: paired event + counter via
        # safe_emit; policy_kind derived from policy.kind so cloud
        # team-policy denials get policy_kind=cloud (field) / =team (tag).
        field_kind, tag_kind = _policy_kind_fields(policy)
        safe_emit(
            logger, logging.INFO, "policy.denied",
            identity_label=identity.label,
            identity_extra=identity.extra or None,
            resource=resource, action=action, result="denied",
            detail={"policy_kind": field_kind},
            metric_name="cassetta.policy.decisions",
            metric_tags={"result": "denied", "policy_kind": tag_kind},
            metrics=metrics,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden"
        )


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


def _listing_entry(path: str, meta: dict[str, Any], ttl: int) -> dict[str, object]:
    files = meta.get("files", [])
    total_size = sum(int(f.get("size", 0)) for f in files)
    created_at_iso = str(meta.get("created_at", ""))
    remaining = _compute_remaining_ttl(created_at_iso, ttl)
    return {
        "path": path,
        "bundle_id": meta.get("bundle_id", ""),
        "size": total_size,
        "sender": meta.get("sender"),
        "created_at": created_at_iso,
        "remaining_ttl": remaining,
        "file_count": int(meta.get("file_count", len(files))),
        "schema_version": int(meta.get("schema_version", SCHEMA_VERSION)),
        "files": [
            {"name": f["name"], "size": int(f["size"]), "mime": f["mime"]}
            for f in files
        ],
    }


async def _collect_bundle_files(
    backend: StorageBackend, bundle_path: str, records: list[dict[str, Any]]
) -> list[tuple[str, bytes]]:
    result: list[tuple[str, bytes]] = []
    for record in records:
        handle = await backend.open_bundle_file_read(bundle_path, record["name"])
        try:
            data = handle.read()
        finally:
            handle.close()
        result.append((record["name"], data))
    return result


@router.put("/inbox/{agent}/{path:path}")
async def send_to_inbox_removed(
    agent: str,
    path: str,
) -> JSONResponse:
    """Removed endpoint — always returns 410 Gone.

    The legacy inline send (``PUT /inbox/{agent}/{path}``) has been replaced by the
    two-phase upload flow. Send via ``POST /upload/{bundle_path}`` instead; the response
    body's ``migration_guide`` points at the migration notes.
    """
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            "error": "gone",
            "reason": "replaced_by_514",
            "replacement": "POST /upload/{bundle_path}",
            "migration_guide": "MIGRATION.md#brief-514",
        },
    )


@router.get("/inbox/{agent}/", response_model=InboxListResponse)
async def list_inbox(
    agent: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
    prefix: str = "",
) -> InboxListResponse:
    config = _get_config(request)
    await _enforce(request, identity, f"inbox:{agent}", "list", metrics)
    # Brief 533 FR-001: hot-path coverage — emit at START of post-enforce body.
    safe_emit(
        metric_name="cassetta.inbox.operations",
        metric_tags={"action": "list"},
        metrics=metrics,
    )
    scan_prefix = f"{INBOX_NAMESPACE}/{agent}/"
    if prefix:
        scan_prefix = f"{INBOX_NAMESPACE}/{agent}/{prefix}"

    # Brief 515: hide bundles with an active reference-mode claim (FR-013).
    policy = _get_limits_policy(request)
    claim_store = _get_claim_store(request)
    ttls = policy.ttls(PolicyContext(identity=identity))
    hidden = set(
        await claim_store.iter_active_by_bundle_path(int(ttls["download_claim_ttl"])),
    )

    entries: list[dict[str, object]] = []
    for ref in backend.list_bundles(scan_prefix):
        if ref.path in hidden:
            continue
        try:
            meta = await backend.read_bundle_meta(ref.path)
        except FileNotFoundError:
            struct_log(
                logger, logging.WARNING, "inbox.foreign_object",
                resource=f"inbox:{agent}", detail={"path": ref.path},
            )
            continue
        created_at_iso = str(meta.get("created_at", ""))
        if _is_expired_by_meta(created_at_iso, config.default_ttl):
            continue
        display_path = ref.path.removeprefix(f"{INBOX_NAMESPACE}/{agent}/")
        entries.append(_listing_entry(display_path, meta, config.default_ttl))

    entries.sort(key=lambda f: str(f["created_at"]), reverse=True)

    return InboxListResponse(
        agent=agent,
        files=[InboxFileInfo(**e) for e in entries],  # type: ignore[arg-type]
    )


@router.get("/inbox/{agent}/{path:path}/peek", response_model=PeekResponse)
async def peek_inbox_file(
    agent: str,
    path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> JSONResponse:
    config = _get_config(request)
    await _enforce(request, identity, f"inbox:{agent}", "peek", metrics)
    bundle_path = f"{INBOX_NAMESPACE}/{agent}/{path}"

    try:
        meta = await backend.read_bundle_meta(bundle_path)
    except FileNotFoundError as exc:
        struct_log(
            logger, logging.INFO, "peek.not_found",
            identity_label=identity.label,
            resource=f"inbox:{agent}",
            detail={"path": path, "found": False, "reason": "absent"},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        ) from exc

    if _is_expired_by_meta(str(meta.get("created_at", "")), config.default_ttl):
        struct_log(
            logger, logging.INFO, "peek.not_found",
            identity_label=identity.label,
            resource=f"inbox:{agent}",
            detail={"path": path, "found": False, "reason": "expired"},
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )

    file_count = int(meta.get("file_count", 0))
    total_size = sum(int(f.get("size", 0)) for f in meta.get("files", []))
    struct_log(
        logger, logging.DEBUG, "peek.ok",
        identity_label=identity.label,
        resource=f"inbox:{agent}",
        detail={
            "path": path, "found": True, "file_count": file_count,
            "total_size": total_size, "bundle_id": meta.get("bundle_id"),
        },
    )
    safe_emit(
        metric_name="cassetta.inbox.operations",
        metric_tags={"action": "peek"},
        metrics=metrics,
    )
    return JSONResponse(
        content=PeekResponse(bundle=meta).model_dump(),
        headers={
            "X-Cassetta-Sender": str(meta.get("sender") or ""),
            "X-Cassetta-File-Count": str(file_count),
        },
    )


@router.get("/inbox/{agent}/{path:path}")
async def get_inbox_file(
    agent: str,
    path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> Response:
    config = _get_config(request)
    await _enforce(request, identity, f"inbox:{agent}", "read", metrics)
    # Brief 533 FR-001 / SC-014: emit at START so 404 still increments.
    safe_emit(
        metric_name="cassetta.inbox.operations",
        metric_tags={"action": "read"},
        metrics=metrics,
    )
    bundle_path = f"{INBOX_NAMESPACE}/{agent}/{path}"

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
    sender = meta.get("sender", "")
    records = list(meta.get("files", []))

    # Policy decides inline vs reference.
    policy = _get_limits_policy(request)
    total_size = sum(int(f.get("size", 0)) for f in records)
    entry: DownloadEntry = {"file_count": file_count, "total_size": total_size}
    decision = await policy.evaluate_download(
        PolicyContext(identity=identity), entry,
    )
    mode = decision.get("mode")
    struct_log(
        logger, logging.DEBUG, "download_mode_decision",
        identity_label=identity.label,
        detail={"bundle_id": meta.get("bundle_id"), "mode": mode,
                "namespace": "inbox"},
    )

    if mode == "reference":
        transport = _get_transport(request)
        claim_store = _get_claim_store(request)
        ref_envelope = await build_reference_payload_for_inbox(
            config=_get_config(request), policy=policy, transport=transport,
            claim_store=claim_store, identity=identity,
            bundle_path=bundle_path, meta=meta,
            recipient=agent,
            write_claim=False,
        )
        return JSONResponse(
            content=dict(ref_envelope),
            headers={
                "X-Cassetta-Sender": sender or "",
                "X-Cassetta-File-Count": str(file_count),
            },
        )

    payloads = await _collect_bundle_files(backend, bundle_path, records)
    inline_envelope = build_inline_envelope(meta, payloads)
    return JSONResponse(
        content=dict(inline_envelope),
        headers={
            "X-Cassetta-Sender": sender or "",
            "X-Cassetta-File-Count": str(file_count),
        },
    )


@router.post("/inbox/{agent}/{path:path}/pick")
async def pick_inbox_file(
    agent: str,
    path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> Response:
    config = _get_config(request)
    await _enforce(request, identity, f"inbox:{agent}", "pick", metrics)
    # Brief 533 FR-001: emit at START of post-enforce body.
    safe_emit(
        metric_name="cassetta.inbox.operations",
        metric_tags={"action": "pick"},
        metrics=metrics,
    )

    if path == "latest":
        scan_prefix = f"{INBOX_NAMESPACE}/{agent}/"
        newest_path: str | None = None
        newest_time = ""
        newest_meta: dict[str, Any] | None = None
        for ref in backend.list_bundles(scan_prefix):
            try:
                meta = await backend.read_bundle_meta(ref.path)
            except FileNotFoundError:
                struct_log(
                    logger, logging.WARNING, "inbox.foreign_object",
                    resource=f"inbox:{agent}", detail={"path": ref.path},
                )
                continue
            created_at_iso = str(meta.get("created_at", ""))
            if _is_expired_by_meta(created_at_iso, config.default_ttl):
                continue
            if created_at_iso > newest_time:
                newest_time = created_at_iso
                newest_path = ref.path
                newest_meta = meta
        if newest_path is None or newest_meta is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No files in inbox",
            )
        bundle_path = newest_path
        meta = newest_meta
        actual_path = newest_path.removeprefix(f"{INBOX_NAMESPACE}/{agent}/")
    else:
        bundle_path = f"{INBOX_NAMESPACE}/{agent}/{path}"
        actual_path = path
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
    sender = meta.get("sender", "")
    records = list(meta.get("files", []))
    total_size = sum(int(f.get("size", 0)) for f in records)

    policy = _get_limits_policy(request)
    entry: DownloadEntry = {"file_count": file_count, "total_size": total_size}
    decision = await policy.evaluate_download(
        PolicyContext(identity=identity), entry,
    )
    mode = decision.get("mode")
    struct_log(
        logger, logging.DEBUG, "download_mode_decision",
        identity_label=identity.label,
        detail={"bundle_id": meta.get("bundle_id"), "mode": mode,
                "namespace": "inbox"},
    )

    if mode == "reference":
        transport = _get_transport(request)
        claim_store = _get_claim_store(request)
        try:
            ref_envelope = await build_reference_payload_for_inbox(
                config=_get_config(request), policy=policy, transport=transport,
                claim_store=claim_store, identity=identity,
                bundle_path=bundle_path, meta=meta,
                recipient=agent, write_claim=True,
            )
        except BundleClaimedError as exc:
            # Same "not found" surface as any late-comer (FR-011a step 6).
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File not found: {path}",
            ) from exc
        return JSONResponse(
            content=dict(ref_envelope),
            headers={
                "X-Cassetta-Sender": sender or "",
                "X-Cassetta-Path": actual_path,
                "X-Cassetta-File-Count": str(file_count),
            },
        )

    payloads = await _collect_bundle_files(backend, bundle_path, records)

    try:
        await backend.delete_bundle(bundle_path)
    except FileNotFoundError:
        pass

    inline_envelope = build_inline_envelope(meta, payloads)
    return JSONResponse(
        content=dict(inline_envelope),
        headers={
            "X-Cassetta-Sender": sender or "",
            "X-Cassetta-Path": actual_path,
            "X-Cassetta-File-Count": str(file_count),
        },
    )


@router.delete("/inbox/{agent}/{path:path}", status_code=204)
async def delete_inbox_file(
    agent: str,
    path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> Response:
    await _enforce(request, identity, f"inbox:{agent}", "delete", metrics)
    # Brief 533 FR-001: emit at START of post-enforce body.
    safe_emit(
        metric_name="cassetta.inbox.operations",
        metric_tags={"action": "delete"},
        metrics=metrics,
    )
    bundle_path = f"{INBOX_NAMESPACE}/{agent}/{path}"

    try:
        await backend.delete_bundle(bundle_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        ) from exc

    return Response(status_code=204)


def register_conflict_handler(_app: Any) -> None:
    """Placeholder for exception-handler registration (see app.py)."""


def build_bundle_conflict_response(exc: BundlePathConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=BundlePathConflict(
            conflicting_path=exc.conflicting_path,
            kind=exc.kind,
        ).model_dump(),
    )
