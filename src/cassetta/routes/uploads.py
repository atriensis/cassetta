"""REST endpoint ``POST /uploads`` — create a directed-send upload session.

Phase-1 of the two-phase send over plain HTTP: authenticate the caller with
their API key, validate the manifest and destination, run the recipient and
capacity policies, and return where to stream the bundle bytes plus the
short-lived credential to use. Phase-2 (``POST /upload/{bundle_path}``) is
unchanged. The transport-neutral work lives in :func:`cassetta.send_init.
prepare_send_init`, shared with the MCP send-init tool, so both surfaces
behave identically; this route only adapts HTTP request/response and maps the
typed errors to status codes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from cassetta.auth.dependencies import get_current_identity
from cassetta.dependencies import (
    get_access_policy,
    get_alias_resolver,
    get_limits_policy,
    get_metrics,
)
from cassetta.models import UploadSession, UploadSessionRequest
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.alias import AliasResolver
from cassetta.protocols.config import CoreConfig
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import LimitsPolicy
from cassetta.protocols.metrics import MetricsProvider
from cassetta.send_init import (
    AccessDenied,
    ManifestError,
    UnknownRecipientError,
    prepare_send_init,
)

router = APIRouter()


@router.post(
    "/uploads",
    response_model=UploadSession,
    status_code=status.HTTP_201_CREATED,
)
async def create_upload_session(
    payload: UploadSessionRequest,
    request: Request,
    identity: Annotated[Identity, Depends(get_current_identity)],
    access_policy: Annotated[AccessPolicy, Depends(get_access_policy)],
    alias_resolver: Annotated[AliasResolver, Depends(get_alias_resolver)],
    limits_policy: Annotated[LimitsPolicy, Depends(get_limits_policy)],
    metrics: Annotated[MetricsProvider, Depends(get_metrics)],
) -> UploadSession:
    """Start a directed upload session.

    Validates the manifest and destination, confirms the caller may send to
    the recipient, then returns where to upload the bundle bytes and the
    short-lived credential to use. Stream the tar archive to ``upload_url``
    carrying ``batch_token`` to complete the send.
    """
    config: CoreConfig = request.app.state.config
    try:
        result = await prepare_send_init(
            to=payload.to,
            path=payload.path,
            manifest=payload.manifest,
            identity=identity,
            sender_label=identity.label,
            config=config,
            access_policy=access_policy,
            alias_resolver=alias_resolver,
            limits_policy=limits_policy,
            metrics=metrics,
            signing_key=config.jwt_primary_key,
            allow_inline=False,
        )
    except ManifestError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except UnknownRecipientError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    # A capacity overage raises ``LimitsRejection``; it is intentionally not
    # caught here so the app-level handler maps it (413 capacity / 422
    # batch-required), identically to the byte-upload route.

    # ``allow_inline=False`` guarantees batch mode with a concrete upload URL.
    assert result.upload_url is not None
    return UploadSession(
        mode="batch",
        bundle_id=result.bundle_id,
        upload_url=result.upload_url,
        batch_token=result.token,
        expires_at=result.expires_at,
    )
