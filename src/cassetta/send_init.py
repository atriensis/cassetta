"""Transport-agnostic phase-1 of the directed two-phase send (``send-init``).

A single Layer-2 helper, :func:`prepare_send_init`, shared by the MCP tool
``cassetta_send_init`` and the REST route ``POST /uploads``. It validates the
manifest, resolves the recipient, runs the access and capacity policies, mints a
short-lived upload credential, and returns a :class:`SendInitResult`. Every
collaborator is a parameter — no transport/request state, no module globals, no
vendor imports — so both surfaces share one code path (mirrors
:mod:`cassetta.capabilities`).
"""

from __future__ import annotations

import logging
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from cassetta.auth import jwt_tokens
from cassetta.auth.manifest_validation import validate_manifest
from cassetta.config import AppConfig
from cassetta.defaults.default_limits import LimitsRejection, _format_reason
from cassetta.mime import pick_mime
from cassetta.models import SendManifest
from cassetta.path_validation import PathValidationError, validate_path
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.alias import AliasResolver
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import (
    LimitsPolicy,
    ManifestFile,
    PolicyContext,
    UploadManifest,
)
from cassetta.protocols.metrics import MetricsProvider
from cassetta.structured_log import safe_emit, struct_log

logger = logging.getLogger("cassetta")

INBOX_NAMESPACE = "inbox"


# --- Errors -----------------------------------------------------------------
#
# Every error subclasses ValueError and carries the same stable-prefix message
# the MCP surface already returns to callers, so the MCP tool surfaces identical
# error text without any new conversion. The REST route maps these by TYPE to
# HTTP status (see routes/uploads.py). Capacity overage reuses the existing
# ``LimitsRejection`` (it has an app-level handler → HTTP 413/422).


class SendInitError(ValueError):
    """Base for send-init failures (subclass of :class:`ValueError`)."""


class ManifestError(SendInitError):
    """Manifest or destination-path validation failed."""


class UnknownRecipientError(SendInitError):
    """A label-style recipient could not be resolved to an inbox."""


class AccessDenied(SendInitError):
    """The caller is not permitted to send to the resolved recipient."""


# --- Result -----------------------------------------------------------------


@dataclass(frozen=True)
class SendInitResult:
    """Outcome of phase-1.

    ``token`` is the minted upload JWT; callers label it ``inline_token`` (inline)
    or ``batch_token`` (batch). ``upload_url`` is set iff ``mode == "batch"``.
    """

    bundle_id: str
    mode: Literal["inline", "batch"]
    token: str
    expires_at: str
    upload_url: str | None


# --- Pure helpers (moved verbatim from mcp_server; send-init only) -----------


def _manifest_from_input(raw: dict[str, Any]) -> UploadManifest:
    """Parse a free-form ``manifest`` dict into an :class:`UploadManifest`."""
    files_raw = raw.get("files", [])
    if not isinstance(files_raw, list):
        raise ValueError("invalid_manifest: reason=malformed, name=''")
    entries: list[ManifestFile] = []
    for f in files_raw:
        if not isinstance(f, dict):
            raise ValueError("invalid_manifest: reason=malformed, name=''")
        name = str(f.get("name", ""))
        size_val = f.get("size", 0)
        try:
            size = int(size_val)
        except (TypeError, ValueError):
            raise ValueError(f"invalid_manifest: reason=malformed, name={name!r}") from None
        mime_val = f.get("mime")
        entries.append({"name": name, "size": size, "mime": mime_val})
    file_count_val = raw.get("file_count", len(entries))
    try:
        file_count = int(file_count_val)
    except (TypeError, ValueError):
        raise ValueError("invalid_manifest: reason=malformed, name=''") from None
    if file_count != len(entries):
        raise ValueError("invalid_manifest: reason=malformed, name=''")
    return {"file_count": file_count, "files": entries}


def _enrich_manifest_mimes(manifest: UploadManifest) -> UploadManifest:
    """Fill in server-inferred mime types for entries with ``mime == None``."""
    enriched: list[ManifestFile] = []
    for entry in manifest["files"]:
        name = entry["name"]
        mime = entry.get("mime")
        enriched.append(
            {
                "name": name,
                "size": entry["size"],
                "mime": pick_mime(name, explicit=mime),
            }
        )
    return {"file_count": manifest["file_count"], "files": enriched}


def _recipient_of(target: str) -> str:
    """Extract the recipient name from an alias-resolved target like ``inbox/alice/``."""
    return target.rstrip("/").removeprefix("inbox/").rstrip("/")


# --- Phase-1 helper ---------------------------------------------------------


async def prepare_send_init(
    *,
    to: str,
    path: str,
    manifest: SendManifest,
    identity: Identity,
    sender_label: str | None,
    config: AppConfig,
    access_policy: AccessPolicy,
    alias_resolver: AliasResolver | None,
    limits_policy: LimitsPolicy,
    metrics: MetricsProvider,
    signing_key: bytes,
    allow_inline: bool,
) -> SendInitResult:
    """Run phase-1 of a directed send and return an upload session descriptor.

    Order mirrors the original MCP ``send_init``: validate manifest → validate
    leaf path → resolve recipient → access check → capacity policy → mint JWT.

    When ``allow_inline`` is False the result is always ``batch`` (the capacity
    policy still runs, so caps are enforced identically) — used by the REST
    surface, which has no inline-completion step. When True the policy's mode is
    honoured (MCP keeps inline-for-small).

    Raises :class:`ManifestError`, :class:`UnknownRecipientError`,
    :class:`AccessDenied`, or :class:`LimitsRejection`.
    """
    # Step 1: parse + validate manifest.
    try:
        parsed_manifest = _manifest_from_input(manifest.model_dump(exclude_none=True))
        validate_manifest(parsed_manifest)
    except ValueError as exc:
        raise ManifestError(str(exc)) from exc

    # Step 2: validate the bundle leaf path (same allowed-chars set as file names).
    try:
        validate_path(path, allowed_chars=config.allowed_path_chars)
    except PathValidationError as exc:
        raise ManifestError(f"invalid_manifest: reason=invalid_char, name={path!r}") from exc

    # Step 3: resolve recipient via the alias resolver (unicast/multicast).
    if alias_resolver is not None:
        resolved = await alias_resolver.resolve(to, sender_label=sender_label)
        if resolved is None:
            # A label-style ``to`` with no matching key is an unknown recipient;
            # a bare name passes through (legacy direct addressing). The ``":"``
            # heuristic mirrors the alias resolvers.
            if ":" in to:
                raise UnknownRecipientError(f"unknown_recipient: name={to!r}")
            recipient = to
        else:
            targets = resolved.inbox_targets
            if len(targets) > 1 and sender_label is not None:
                targets = [t for t in targets if not t.rstrip("/").endswith(sender_label)]
            recipient = _recipient_of(targets[0])
    else:
        recipient = to

    # Step 4: access / recipient-visibility check.
    resource = f"{INBOX_NAMESPACE}:{recipient}"
    if not await access_policy.check(identity, resource, "write"):
        _kind = getattr(access_policy, "kind", "core")
        _field_kind = _kind if _kind in ("core", "cloud") else "core"
        _tag_kind = "team" if _field_kind == "cloud" else "core"
        safe_emit(
            logger,
            logging.INFO,
            "policy.denied",
            identity_label=identity.label,
            identity_extra=identity.extra or None,
            resource=resource,
            action="write",
            result="denied",
            detail={"policy_kind": _field_kind},
            metric_name="cassetta.policy.decisions",
            metric_tags={"result": "denied", "policy_kind": _tag_kind},
            metrics=metrics,
        )
        raise AccessDenied("Forbidden")

    # Step 5: capacity policy (raises LimitsRejection on cap_exceeded).
    ctx = PolicyContext(identity=identity)
    decision = await limits_policy.evaluate_upload(ctx, parsed_manifest)
    if "error" in decision:
        constraint = decision["constraint"]
        context: str | None = None
        if constraint == "per_file_max":
            for f in parsed_manifest["files"]:
                if f["size"] == decision["observed"]:
                    context = f"file {f['name']!r}"
                    break
        reason = _format_reason(
            decision["error"],
            constraint,
            decision.get("limit"),
            decision["observed"],
            context=context,
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

    policy_mode = decision.get("mode")
    if policy_mode not in ("inline", "batch"):
        raise ValueError(f"policy returned unexpected mode: {policy_mode!r}")
    # REST surface forces batch (no inline-completion step); MCP honours the policy.
    # The ternary reconstructs a precise Literal: ``decision.get("mode")`` is typed
    # ``UploadMode | list[UploadMode]``, which the ``not in`` guard cannot narrow.
    mode: Literal["inline", "batch"] = ("inline" if policy_mode == "inline" else "batch") if allow_inline else "batch"

    # Step 6: mint the upload JWT.
    enriched_manifest = _enrich_manifest_mimes(parsed_manifest)
    bundle_id = str(uuid.uuid4())
    bundle_path = f"{INBOX_NAMESPACE}/{recipient}/{path}"
    now = int(time.time())
    ttl = limits_policy.ttls(ctx)["upload_token_ttl"]
    sender = sender_label or identity.label
    claims = {
        "bundle_path": bundle_path,
        "bundle_id": bundle_id,
        "sender": sender,
        "recipient": recipient,
        "mode": mode,
        "manifest": enriched_manifest,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
    }
    token = jwt_tokens.sign(claims, key=signing_key)
    expires_at = datetime.fromtimestamp(now + ttl, UTC).isoformat().replace("+00:00", "Z")

    total_size = sum(f["size"] for f in enriched_manifest["files"])
    struct_log(
        logger,
        logging.INFO,
        "upload_init",
        identity_label=identity.label,
        detail={
            "bundle_id": bundle_id,
            "bundle_path": bundle_path,
            "mode": mode,
            "sender": sender_label,
            "recipient": recipient,
            "file_count": enriched_manifest["file_count"],
            "total_size": total_size,
        },
    )
    safe_emit(
        metric_name="cassetta.inbox.operations",
        metric_tags={"action": "send"},
        metrics=metrics,
    )

    if mode == "inline":
        return SendInitResult(
            bundle_id=bundle_id,
            mode="inline",
            token=token,
            expires_at=expires_at,
            upload_url=None,
        )
    encoded_path = urllib.parse.quote(bundle_path, safe="")
    upload_url = f"{config.public_base_url.rstrip('/')}/upload/{encoded_path}"
    return SendInitResult(
        bundle_id=bundle_id,
        mode="batch",
        token=token,
        expires_at=expires_at,
        upload_url=upload_url,
    )
