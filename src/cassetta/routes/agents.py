"""REST endpoints for agent discovery and broadcast.

Visibility-first broadcast pipeline:
    validate_path → limits → list_keys → visible_agents → resolver →
    policy.check (per-target) → write → metrics + log.
"""

import io
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.datastructures import UploadFile

from cassetta.auth import get_current_identity, get_key_store
from cassetta.config import AppConfig
from cassetta.dependencies import (
    get_access_policy,
    get_alias_resolver,
    get_limits_policy,
    get_metrics,
)
from cassetta.mime import pick_mime
from cassetta.path_validation import PathValidationError, validate_path
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.alias import AliasResolver
from cassetta.protocols.identity import Identity
from cassetta.protocols.keystore import KeyStoreProtocol
from cassetta.protocols.limits import (
    LimitsPolicy,
    ManifestFile,
    PolicyContext,
    UploadManifest,
)
from cassetta.protocols.metrics import MetricsProvider
from cassetta.protocols.storage import StorageBackend
from cassetta.rate_limit.limiter import (
    _check_fanout_cap,
    check_rate_limit_imperative,
)
from cassetta.structured_log import safe_emit, struct_log

logger = logging.getLogger("cassetta")

router = APIRouter()

SCHEMA_VERSION = 1
INBOX_NAMESPACE = "inbox"


def _get_backend(request: Request) -> StorageBackend:
    return request.app.state.backends.backend  # type: ignore[no-any-return]


def _get_config(request: Request) -> AppConfig:
    return request.app.state.config  # type: ignore[no-any-return]


def _sender_from_identity(identity: Identity) -> str | None:
    if identity.extra.get("dev") or identity.extra.get("operator"):
        return None
    return identity.label


async def _write_inbox_bundle(
    backend: StorageBackend,
    bundle_path: str,
    files: list[tuple[str, bytes, str | None]],
    *,
    sender: str | None,
    bundle_id: str,
) -> None:
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
            "sender": sender,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": len(records),
            "files": records,
        }
        await writer.commit(meta)
    except Exception:
        await writer.abort()
        raise


@router.get("/agents")
async def list_agents(
    key_store: Annotated[KeyStoreProtocol, Depends(get_key_store)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    access_policy: Annotated[AccessPolicy, Depends(get_access_policy)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> dict[str, object]:
    """List the agents visible to the authenticated caller.

    Returns ``{"agents": [{"label", "created_at"}, ...]}``, restricted to the agents the
    caller is permitted to see; agents outside the caller's visibility are omitted.
    """
    keys = await key_store.list_keys()
    active = [k for k in keys if k.is_active]
    labels = [k.label for k in active]
    try:
        visible_labels = await access_policy.visible_agents(identity, labels)
    except Exception:
        struct_log(
            logger,
            logging.ERROR,
            "agents.visibility_failed",
            identity_label=identity.label,
            result="error",
        )
        visible_labels = []

    visible_set = set(visible_labels)
    visible_keys = [k for k in active if k.label in visible_set]
    agents = [{"label": k.label, "created_at": k.created_at.isoformat()} for k in visible_keys]

    result_tag = "unfiltered" if len(visible_labels) == len(labels) else "filtered"
    safe_emit(
        metric_name="cassetta.agents.list",
        metric_tags={"result": result_tag},
        metrics=metrics,
    )

    return {"agents": agents}


@router.post("/broadcast/{path:path}")
async def broadcast(
    path: str,
    request: Request,
    backend: Annotated[StorageBackend, Depends(_get_backend)],
    key_store: Annotated[KeyStoreProtocol, Depends(get_key_store)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
    limits_policy: Annotated[LimitsPolicy, Depends(get_limits_policy)],
    access_policy: Annotated[AccessPolicy, Depends(get_access_policy)],
    alias_resolver: Annotated[AliasResolver, Depends(get_alias_resolver)],
) -> dict[str, object]:
    """Send a file or bundle to every recipient visible to the caller.

    The request body carries the payload (raw bytes, or a multipart form for a
    multi-file bundle), exactly as for a directed send. The file is delivered to each
    visible recipient's inbox and the response summarises the fan-out. Subject to the
    per-IP broadcast rate limit and the fan-out cap on the number of recipients.
    """
    config = _get_config(request)
    # Per-IP rate limit, sharing one budget with MCP.
    # The unified RateLimitExceeded handler at app.py emits the 429
    # envelope and increments cassetta.rate_limit.hits{route=broadcast}.
    check_rate_limit_imperative(request, config.rate_limit_broadcast)
    # 0) Validate path.
    try:
        validate_path(path, allowed_chars=config.allowed_path_chars)
    except PathValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid path: {exc}",
        ) from exc

    sender = _sender_from_identity(identity)

    # Parse body.
    content_type = request.headers.get("content-type", "")
    is_multipart = "multipart/form-data" in content_type

    if is_multipart:
        form = await request.form()
        file_parts: list[tuple[str, bytes, str | None]] = []
        for _key, upload in form.multi_items():
            if isinstance(upload, UploadFile) and upload.filename:
                file_content = await upload.read()
                explicit = upload.content_type if upload.content_type else None
                file_parts.append((upload.filename, file_content, explicit))
    else:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
        content_bytes = bytes(body)
        file_parts = [(path.split("/")[-1], content_bytes, None)]

    # 1) Limits check — raises LimitsRejection (handled at app.py:210 → 413/422).
    manifest_files: list[ManifestFile] = [
        {"name": name, "size": len(data), "mime": mime} for name, data, mime in file_parts
    ]
    manifest: UploadManifest = {
        "file_count": len(manifest_files),
        "files": manifest_files,
    }
    ctx = PolicyContext(identity=identity)
    decision = await limits_policy.evaluate_upload(ctx, manifest)
    if "error" in decision:
        from cassetta.defaults.default_limits import LimitsRejection, _format_reason

        constraint = decision["constraint"]
        reason = _format_reason(
            decision["error"],
            constraint,
            decision.get("limit"),
            decision["observed"],
        )
        raise LimitsRejection(
            error=decision["error"],
            constraint=constraint,
            limit=decision.get("limit"),
            observed=decision["observed"],
            reason=reason,
        )

    # 2) Active labels excluding sender.
    keys = await key_store.list_keys()
    candidate_labels = [k.label for k in keys if k.is_active and k.label != sender]

    # 2a) Fan-out cap. Reject before any storage
    # write. The handler at app.py emits the structured 429 envelope
    # and increments the rate-limit counter with reason=fanout_cap.
    _check_fanout_cap(
        target_count=len(candidate_labels),
        max_targets=config.broadcast_max_targets,
    )

    # 3) Visibility filter — fail-closed on exception.
    visibility_failed = False
    try:
        visible_labels = await access_policy.visible_agents(
            identity,
            candidate_labels,
        )
    except Exception:
        struct_log(
            logger,
            logging.ERROR,
            "broadcast.visibility_failed",
            identity_label=identity.label,
            detail={"path": path, "sender": sender},
        )
        visible_labels = []
        visibility_failed = True

    delivered: list[str] = []
    denied: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    for label in visible_labels:
        # 4) Resolve.
        try:
            resolved = await alias_resolver.resolve(
                label,
                sender_label=sender,
            )
        except Exception as exc:
            failed.append(
                {
                    "target": label,
                    "error": f"resolver_error: {exc}",
                }
            )
            continue
        if resolved is None:
            failed.append({"target": label, "error": "resolver_unknown"})
            continue

        # 5) Policy check.
        try:
            allowed = await access_policy.check(
                identity,
                f"inbox:{label}",
                "write",
            )
        except Exception:
            denied.append({"target": label, "reason": "check_error"})
            continue
        if not allowed:
            denied.append({"target": label, "reason": "access_denied"})
            # policy_kind derived from access_policy.kind so cloud
            # team-policy denials are tagged policy_kind=cloud (field)
            # / =team (counter tag).
            _kind = getattr(access_policy, "kind", "core")
            _field_kind = _kind if _kind in ("core", "cloud") else "core"
            _tag_kind = "team" if _field_kind == "cloud" else "core"
            safe_emit(
                logger,
                logging.INFO,
                "policy.denied",
                identity_label=identity.label,
                resource=f"inbox:{label}",
                action="write",
                result="denied",
                detail={"policy_kind": _field_kind},
                metric_name="cassetta.policy.decisions",
                metric_tags={
                    "result": "denied",
                    "action": "write",
                    "policy_kind": _tag_kind,
                },
                metrics=metrics,
            )
            continue

        # 6) Write — `inbox_targets` already carries the inbox/<recipient>/ prefix.
        try:
            for target in resolved.inbox_targets:
                bundle_path = f"{target.rstrip('/')}/{path}"
                try:
                    await backend.delete_bundle(bundle_path)
                except FileNotFoundError:
                    pass
                bundle_id = uuid.uuid4().hex
                await _write_inbox_bundle(
                    backend,
                    bundle_path,
                    file_parts,
                    sender=sender,
                    bundle_id=bundle_id,
                )
            delivered.append(label)
        except Exception as exc:
            failed.append(
                {
                    "target": label,
                    "error": f"write_error: {exc}",
                }
            )

    # 7) Result-tag matrix (R11) + log + response.
    if visibility_failed:
        result = "error"
    elif not visible_labels:
        result = "noop"
    elif delivered and (denied or failed):
        result = "partial"
    elif delivered:
        result = "success"
    else:
        result = "error"

    safe_emit(
        logger,
        logging.INFO,
        "broadcast.sent",
        identity_label=identity.label,
        result=result,
        detail={
            "path": path,
            "sender": sender,
            "delivered": len(delivered),
            "denied": len(denied),
            "failed": len(failed),
        },
        metric_name="cassetta.broadcast.operations",
        metric_tags={"result": result},
        metrics=metrics,
    )

    return {
        "path": path,
        "delivered_to": delivered,
        "denied": denied,
        "failed": failed,
        "total_delivered": len(delivered),
        "total_denied": len(denied),
        "total_failed": len(failed),
    }
