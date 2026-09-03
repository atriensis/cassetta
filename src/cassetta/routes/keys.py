import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from cassetta.auth import get_current_identity, get_key_store
from cassetta.dependencies import get_metrics
from cassetta.models import (
    KeyCreateRequest,
    KeyCreateResponse,
    KeyListResponse,
    RevokedResponse,
    SetupRequest,
)
from cassetta.models import KeyInfo as KeyInfoModel
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.identity import Identity
from cassetta.protocols.keystore import KeyStoreProtocol
from cassetta.protocols.metrics import MetricsProvider
from cassetta.structured_log import safe_emit

logger = logging.getLogger("cassetta")

router = APIRouter()


def _get_policy(request: Request) -> AccessPolicy:
    return request.app.state.backends.access_policy  # type: ignore[no-any-return]


def _policy_kind_fields(policy: AccessPolicy) -> tuple[str, str]:
    """See ``routes/inbox.py:_policy_kind_fields``."""
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
        # Paired event + counter via safe_emit;
        # policy_kind derived from policy.kind.
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


@router.post("/setup", status_code=201)
async def setup(
    body: SetupRequest,
    request: Request,
    key_store: Annotated[KeyStoreProtocol, Depends(get_key_store)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> KeyCreateResponse:
    await _enforce(request, identity, f"keys:{body.label}", "create", metrics)
    try:
        raw_key, info = await key_store.setup(body.label)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    safe_emit(
        logger,
        logging.INFO,
        "key.created",
        identity_label=identity.label,
        resource=f"keys:{body.label}",
        action="create",
        result="ok",
        detail={"setup": True},
        metric_name="cassetta.keys.operations",
        metric_tags={"action": "create"},
        metrics=metrics,
    )
    keys = await key_store.list_keys()
    safe_emit(
        metric_name="cassetta.active_keys",
        metric_value=float(sum(1 for k in keys if k.is_active)),
        metrics=metrics,
        gauge=True,
    )
    return KeyCreateResponse(
        label=info.label,
        api_key=raw_key,
        created_at=info.created_at,
    )


@router.post("/keys", status_code=201)
async def create_key(
    body: KeyCreateRequest,
    request: Request,
    key_store: Annotated[KeyStoreProtocol, Depends(get_key_store)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> KeyCreateResponse:
    await _enforce(request, identity, f"keys:{body.label}", "create", metrics)
    try:
        raw_key, info = await key_store.create_key(
            body.label,
            user_id=body.user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    safe_emit(
        logger,
        logging.INFO,
        "key.created",
        identity_label=identity.label,
        resource=f"keys:{body.label}",
        action="create",
        result="ok",
        metric_name="cassetta.keys.operations",
        metric_tags={"action": "create"},
        metrics=metrics,
    )
    keys = await key_store.list_keys()
    safe_emit(
        metric_name="cassetta.active_keys",
        metric_value=float(sum(1 for k in keys if k.is_active)),
        metrics=metrics,
        gauge=True,
    )
    return KeyCreateResponse(
        label=info.label,
        api_key=raw_key,
        created_at=info.created_at,
    )


@router.get("/keys")
async def list_keys(
    request: Request,
    key_store: Annotated[KeyStoreProtocol, Depends(get_key_store)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> KeyListResponse:
    await _enforce(request, identity, "keys:*", "list", metrics)
    keys = await key_store.list_keys()
    return KeyListResponse(
        keys=[
            KeyInfoModel(
                label=k.label,
                key_prefix=k.key_prefix,
                created_at=k.created_at,
                is_active=k.is_active,
            )
            for k in keys
        ]
    )


@router.post("/keys/{label}/rotate")
async def rotate_key(
    label: str,
    request: Request,
    key_store: Annotated[KeyStoreProtocol, Depends(get_key_store)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> KeyCreateResponse:
    await _enforce(request, identity, f"keys:{label}", "rotate", metrics)
    try:
        raw_key, info = await key_store.rotate_key(label)
    except KeyError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    safe_emit(
        logger,
        logging.INFO,
        "key.rotated",
        identity_label=identity.label,
        resource=f"keys:{label}",
        action="rotate",
        result="ok",
        metric_name="cassetta.keys.operations",
        metric_tags={"action": "rotate"},
        metrics=metrics,
    )
    # Re-assert the ``cassetta.active_keys`` gauge after rotate — unconditional,
    # even when the count is unchanged, so the gauge stays fresh.
    keys = await key_store.list_keys()
    safe_emit(
        metric_name="cassetta.active_keys",
        metric_value=float(sum(1 for k in keys if k.is_active)),
        metrics=metrics,
        gauge=True,
    )
    return KeyCreateResponse(
        label=info.label,
        api_key=raw_key,
        created_at=info.created_at,
    )


@router.delete("/keys/{label}")
async def revoke_key(
    label: str,
    request: Request,
    key_store: Annotated[KeyStoreProtocol, Depends(get_key_store)],
    identity: Annotated[Identity, Depends(get_current_identity)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> RevokedResponse:
    await _enforce(request, identity, f"keys:{label}", "revoke", metrics)
    try:
        await key_store.revoke_key(label)
    except KeyError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e

    safe_emit(
        logger,
        logging.INFO,
        "key.revoked",
        identity_label=identity.label,
        resource=f"keys:{label}",
        action="revoke",
        result="ok",
        metric_name="cassetta.keys.operations",
        metric_tags={"action": "revoke"},
        metrics=metrics,
    )
    keys = await key_store.list_keys()
    safe_emit(
        metric_name="cassetta.active_keys",
        metric_value=float(sum(1 for k in keys if k.is_active)),
        metrics=metrics,
        gauge=True,
    )
    return RevokedResponse(revoked=label)
