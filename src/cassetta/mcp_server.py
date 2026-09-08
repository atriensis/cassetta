"""MCP server exposing Cassetta file operations as tools.

Tools call the same storage backend as REST endpoints — no code duplication.
"""

import base64
import contextvars
import io
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from cassetta import __version__
from cassetta.auth import jwt_tokens
from cassetta.auth.jwt_hot_reload import JWTKeySlots, _resolve_secondary
from cassetta.capabilities import build_capabilities_document
from cassetta.claims import BundleClaimedError
from cassetta.defaults.default_limits import LimitsRejection, _format_reason
from cassetta.defaults.factory import BackendConfig
from cassetta.downloads import (
    build_reference_payload_for_inbox,
    build_reference_payload_for_store,
)
from cassetta.envelopes import build_inline_envelope
from cassetta.mime import pick_mime
from cassetta.models import SendInlineFile, SendManifest
from cassetta.path_validation import PathValidationError, validate_path
from cassetta.protocols.access import AccessPolicy
from cassetta.protocols.alias import AliasResolver
from cassetta.protocols.claim_storage import ClaimStorage
from cassetta.protocols.config import CoreConfig
from cassetta.protocols.identity import Identity
from cassetta.protocols.keystore import KeyStoreProtocol
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
from cassetta.rate_limit.limiter import (
    FanoutCapExceeded,
    RateLimitExceeded,
    _check_fanout_cap,
    check_rate_limit_imperative,
)
from cassetta.send_init import prepare_send_init
from cassetta.structured_log import safe_emit, struct_log

logger = logging.getLogger("cassetta")

_backend: StorageBackend | None = None
_config: CoreConfig | None = None
_access_policy: AccessPolicy | None = None
_alias_resolver: AliasResolver | None = None
_key_store: KeyStoreProtocol | None = None
_metrics: MetricsProvider | None = None
_limits_policy: LimitsPolicy | None = None
_reference_transport: ReferenceTransport | None = None
_claim_store: ClaimStorage | None = None
# Shared reference to the runtime-mutable JWT key slots.
# Mutated in place by the SIGHUP handler — reads here pick up the new
# primary/secondary on the very next tool call without a re-configure.
_jwt_keys: JWTKeySlots | None = None
_sender_label: contextvars.ContextVar[str | None] = contextvars.ContextVar("sender_label", default=None)
_current_identity: contextvars.ContextVar[Identity | None] = contextvars.ContextVar("current_identity", default=None)

SCHEMA_VERSION = 1
STORE_NAMESPACE = "store"
INBOX_NAMESPACE = "inbox"


def configure(
    config: CoreConfig,
    backends: BackendConfig,
    *,
    jwt_keys: JWTKeySlots | None = None,
) -> None:
    """Wire MCP module globals from ``backends`` and ``config``.

    Called eagerly from ``cassetta.app.lifespan``. Every MCP tool reads
    its dependencies via these globals; calling ``configure`` is a
    precondition for using any tool. Idempotent — a second call
    overwrites the globals with the new bundle's values.

    The optional ``jwt_keys`` argument lets the caller hand in the
    runtime-mutable key holder owned by ``app.state.jwt_keys`` (Brief
    531). Tests that drive the MCP layer without going through
    ``create_app`` may omit it; verify call sites then fall back to
    ``config.jwt_primary_key`` / ``config.jwt_secondary_key``.
    """
    global _backend, _config, _access_policy, _alias_resolver, _key_store, _metrics
    global _limits_policy, _reference_transport, _claim_store, _jwt_keys
    _backend = backends.backend
    _config = config
    _access_policy = backends.access_policy
    _alias_resolver = backends.alias_resolver
    _key_store = backends.key_store
    _metrics = backends.metrics_provider
    _limits_policy = backends.limits_policy
    _reference_transport = backends.reference_transport
    _claim_store = backends.claim_store
    _jwt_keys = jwt_keys


def _verify_keys() -> tuple[bytes, bytes | None]:
    """Return ``(primary, secondary)`` for the verify path.

    Prefers the runtime-mutable ``JWTKeySlots`` set by lifespan; falls
    back to the boot-time configuration values when tests drive the MCP
    layer without going through ``create_app``.
    """
    if _jwt_keys is not None:
        return _jwt_keys.primary, _resolve_secondary(_jwt_keys)
    assert _config is not None, "MCP server not configured"
    return _config.jwt_primary_key, _config.jwt_secondary_key


def set_sender_label(label: str | None) -> None:
    """Set the sender label for MCP tools (from auth middleware)."""
    _sender_label.set(label)


def get_sender_label() -> str | None:
    """Get the sender label for the current request."""
    return _sender_label.get()


def set_current_identity(identity: Identity | None) -> None:
    """Set the resolved Identity for the current MCP request."""
    _current_identity.set(identity)


def get_current_identity() -> Identity | None:
    """Get the resolved Identity for the current MCP request."""
    return _current_identity.get()


def _get_metrics() -> MetricsProvider:
    assert _metrics is not None, "MCP server metrics provider not configured"
    return _metrics


async def _enforce(resource: str, action: str) -> None:
    assert _access_policy is not None, "MCP server access policy not configured"
    policy = _access_policy
    identity = get_current_identity()
    if identity is None:
        identity = Identity(label="dev", extra={"dev": True})
    decision = await policy.check(identity, resource, action)
    if not decision:
        # Paired policy.denied event + counter;
        # policy_kind derived from policy.kind so cloud team-policy
        # denials get policy_kind=cloud (field) / =team (counter tag).
        _kind = getattr(policy, "kind", "core")
        _field_kind = _kind if _kind in ("core", "cloud") else "core"
        _tag_kind = "team" if _field_kind == "cloud" else "core"
        safe_emit(
            logger,
            logging.INFO,
            "policy.denied",
            identity_label=identity.label,
            identity_extra=identity.extra or None,
            resource=resource,
            action=action,
            result="denied",
            detail={"policy_kind": _field_kind},
            metric_name="cassetta.policy.decisions",
            metric_tags={"result": "denied", "policy_kind": _tag_kind},
            metrics=_metrics,
        )
        raise ValueError("Forbidden")


def _get_alias_resolver() -> AliasResolver | None:
    return _alias_resolver


def _get_limits_policy() -> LimitsPolicy:
    assert _limits_policy is not None, "MCP server limits policy not configured"
    return _limits_policy


def _build_upload_manifest(files: list[tuple[str, bytes]]) -> UploadManifest:
    entries: list[ManifestFile] = [{"name": name, "size": len(data), "mime": None} for name, data in files]
    return {"file_count": len(entries), "files": entries}


async def _evaluate_upload_or_raise(
    manifest: UploadManifest,
) -> None:
    """Run the policy on ``manifest``; raise ``LimitsRejection`` on any non-inline result."""
    policy = _get_limits_policy()
    identity = get_current_identity() or Identity(label="dev", extra={"dev": True})
    ctx = PolicyContext(identity=identity)
    decision = await policy.evaluate_upload(ctx, manifest)
    if "error" in decision:
        constraint = decision["constraint"]
        file_name: str | None = None
        if constraint == "per_file_max":
            for f in manifest["files"]:
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
        total_size = sum(f["size"] for f in manifest["files"])
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
            "file_count": manifest["file_count"],
            "total_size": sum(f["size"] for f in manifest["files"]),
            "decision": "inline",
        },
    )


def _get_backend() -> StorageBackend:
    assert _backend is not None, "MCP server not configured"
    return _backend


def _get_config() -> CoreConfig:
    assert _config is not None, "MCP server not configured"
    return _config


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


def _new_bundle_id() -> str:
    return uuid.uuid4().hex


def _parse_files_arg(files: str) -> list[tuple[str, bytes]]:
    file_list = json.loads(files)
    out: list[tuple[str, bytes]] = []
    for entry in file_list:
        name = entry["name"]
        content = entry["content"].encode("utf-8")
        out.append((name, content))
    return out


async def _collect_bundle_files(
    backend: StorageBackend, bundle_path: str, file_records: list[dict[str, Any]]
) -> list[tuple[str, bytes]]:
    result: list[tuple[str, bytes]] = []
    for record in file_records:
        handle = await backend.open_bundle_file_read(bundle_path, record["name"])
        try:
            data = handle.read()
        finally:
            handle.close()
        result.append((record["name"], data))
    return result


def _listing_entry(path: str, meta: dict[str, Any], ttl: int, *, sender: bool) -> dict[str, object]:
    files = meta.get("files", [])
    total_size = sum(int(f.get("size", 0)) for f in files)
    created_at_iso = meta.get("created_at", "")
    remaining = _compute_remaining_ttl(created_at_iso, ttl)
    entry: dict[str, object] = {
        "path": path,
        "bundle_id": meta.get("bundle_id", ""),
        "size": total_size,
        "created_at": created_at_iso,
        "remaining_ttl": remaining,
        "file_count": int(meta.get("file_count", len(files))),
        "schema_version": int(meta.get("schema_version", SCHEMA_VERSION)),
        "files": [{"name": f["name"], "size": int(f["size"]), "mime": f["mime"]} for f in files],
    }
    if sender:
        entry["sender"] = meta.get("sender")
    return entry


async def _write_bundle(
    backend: StorageBackend,
    bundle_path: str,
    files: list[tuple[str, bytes]],
    *,
    sender: str | None,
    content_type: str,
    bundle_id: str,
) -> dict[str, Any]:
    writer = await backend.open_bundle_write(bundle_path)
    try:
        records: list[dict[str, Any]] = []
        for name, data in files:
            await writer.write_file(name, io.BytesIO(data))
            records.append(
                {
                    "name": name,
                    "size": len(data),
                    "mime": pick_mime(name, explicit=None),
                }
            )
        meta: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "bundle_id": bundle_id,
            "sender": sender,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": content_type,
            "file_count": len(records),
            "files": records,
        }
        await writer.commit(meta)
        return meta
    except Exception:
        await writer.abort()
        raise


# === Tool implementations ===


# ---- send_init + send_inline -----------------------------------------------


async def _cassetta_send_init(
    to: str,
    path: str,
    manifest: SendManifest,
) -> dict[str, Any]:
    """Begin sending one or more files to another agent's inbox.

    Pass ``to`` (recipient label, e.g. ``alice:main``), ``path`` (the bundle's
    leaf name under the recipient inbox), and ``manifest`` describing each file:
    ``{files: [{name, size, mime?}], file_count?}`` where ``size`` is the file's
    DECODED byte count.

    Returns a discriminated result: small bundles return
    ``{mode: "inline", inline_token, bundle_id, expires_at}`` — follow up with
    ``cassetta_send_inline``; larger bundles return
    ``{mode: "batch", upload_url, batch_token, bundle_id, expires_at}`` — PUT a
    tar archive to ``upload_url``. The credential expires at ``expires_at``.

    Thin caller of the transport-agnostic :func:`cassetta.send_init.prepare_send_init`;
    the REST route ``POST /uploads`` calls the same helper. MCP keeps inline-for-small
    behaviour (``allow_inline=True``).
    """
    config = _get_config()
    identity = get_current_identity() or Identity(label="dev", extra={"dev": True})
    assert _access_policy is not None, "MCP server access policy not configured"
    try:
        result = await prepare_send_init(
            to=to,
            path=path,
            manifest=manifest,
            identity=identity,
            sender_label=_sender_label.get(),
            config=config,
            access_policy=_access_policy,
            alias_resolver=_get_alias_resolver(),
            limits_policy=_get_limits_policy(),
            metrics=_get_metrics(),
            signing_key=config.jwt_primary_key,
            allow_inline=True,
        )
    except LimitsRejection as exc:
        raise ValueError(str(exc)) from exc

    if result.mode == "inline":
        return {
            "bundle_id": result.bundle_id,
            "mode": "inline",
            "inline_token": result.token,
            "expires_at": result.expires_at,
        }
    return {
        "bundle_id": result.bundle_id,
        "mode": "batch",
        "upload_url": result.upload_url,
        "batch_token": result.token,
        "expires_at": result.expires_at,
    }


def _decode_inline_content(content: str, encoding: str, name: str) -> bytes:
    # An encoding fault — content that cannot be decoded under the declared
    # encoding, or an unsupported encoding — reports a distinct
    # `missing_or_bad_encoding` reason, separable from the genuine size-mismatch
    # check in the caller (which keeps `wrong_size`). The typed SendInlineFile
    # model already rejects a missing/unknown encoding before we reach here, so
    # the trailing raise is defence in depth.
    if encoding == "utf8":
        return content.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise ValueError(f"manifest_violation: reason=missing_or_bad_encoding, name={name!r}") from exc
    raise ValueError(f"manifest_violation: reason=missing_or_bad_encoding, name={name!r}")


def _map_jwt_error(exc: jwt_tokens.TokenError) -> ValueError:
    if isinstance(exc, jwt_tokens.TokenExpired):
        return ValueError("unauthenticated: reason=expired")
    if isinstance(exc, jwt_tokens.TokenNotYetValid):
        return ValueError("unauthenticated: reason=immature")
    if isinstance(exc, jwt_tokens.TokenInvalidSignature):
        return ValueError("unauthenticated: reason=bad_signature")
    if isinstance(exc, jwt_tokens.TokenMissingClaim):
        return ValueError(f"unauthenticated: reason=missing_claim, claim={exc.claim!r}")
    if isinstance(exc, jwt_tokens.RevokedTokenError):
        return ValueError("unauthenticated: reason=revoked")
    return ValueError("unauthenticated: reason=invalid")


async def _cassetta_send_inline(
    token: str,
    files: list[SendInlineFile],
) -> dict[str, Any]:
    """Complete a small (inline) send started by ``cassetta_send_init``.

    Pass the ``inline_token`` from that call as ``token``, and ``files`` as
    ``[{name, content, encoding}]`` where ``encoding`` is ``"base64"`` (arbitrary
    bytes) or ``"utf8"`` (text), ``content`` is the file body encoded that way,
    and the decoded length MUST equal the ``size`` declared in the manifest.
    Every manifest file must be present exactly once. Returns
    ``{bundle_id, ok: true}`` on success.
    """
    backend = _get_backend()

    primary_key, secondary_key = _verify_keys()
    try:
        claims = jwt_tokens.verify(
            token,
            primary=primary_key,
            secondary=secondary_key,
        )
    except jwt_tokens.TokenError as exc:
        raise _map_jwt_error(exc) from exc

    if claims.get("mode") != "inline":
        raise ValueError("unauthenticated: reason=wrong_mode")

    manifest_raw = claims.get("manifest") or {}
    manifest_files: list[dict[str, Any]] = list(manifest_raw.get("files", []))
    by_name = {f["name"]: f for f in manifest_files}

    # Decode + validate sizes per file. The tool advertises a typed
    # list[SendInlineFile]; iterate over the dict form the loop already expects.
    decoded_by_name: dict[str, bytes] = {}
    for entry in (f.model_dump() for f in files):
        name = str(entry.get("name", ""))
        content = str(entry.get("content", ""))
        encoding = str(entry.get("encoding", ""))
        if name not in by_name:
            raise ValueError(f"manifest_violation: reason=extra_file, name={name!r}")
        decoded = _decode_inline_content(content, encoding, name)
        declared = int(by_name[name].get("size", 0))
        if len(decoded) != declared:
            raise ValueError(f"manifest_violation: reason=wrong_size, name={name!r}")
        decoded_by_name[name] = decoded

    missing = set(by_name) - set(decoded_by_name)
    if missing:
        first_missing = sorted(missing)[0]
        raise ValueError(f"manifest_violation: reason=missing_file, name={first_missing!r}")

    bundle_path = str(claims["bundle_path"])
    bundle_id = str(claims["bundle_id"])
    sender = claims.get("sender")

    struct_log(
        logger,
        logging.INFO,
        "upload_stream_start",
        detail={
            "bundle_id": bundle_id,
            "mode": "inline",
            "content_encoding": None,
        },
    )

    try:
        writer = await backend.open_bundle_write(bundle_path)
    except BundlePathConflictError as exc:
        raise ValueError(f"bundle_path_conflict: kind={exc.kind}") from exc
    except FileExistsError as exc:
        # Atomicity: a prior successful commit at this bundle_path blocks this retry.
        raise ValueError("bundle_path_conflict: kind=occupied") from exc

    try:
        bytes_total = 0
        for entry in manifest_files:
            name = entry["name"]
            data = decoded_by_name[name]
            await writer.write_file(name, io.BytesIO(data))
            bytes_total += len(data)
        meta: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "bundle_id": bundle_id,
            "sender": sender,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "bundle" if len(manifest_files) > 1 else "file",
            "file_count": len(manifest_files),
            "files": manifest_files,
        }
        await writer.commit(meta)
    except ValueError as exc:
        await writer.abort()
        struct_log(
            logger,
            logging.WARNING,
            "upload_manifest_violation",
            detail={"bundle_id": bundle_id, "violation": str(exc)},
        )
        struct_log(
            logger,
            logging.WARNING,
            "upload_rollback",
            detail={"bundle_id": bundle_id, "reason": str(exc)},
        )
        raise
    except Exception:
        await writer.abort()
        struct_log(
            logger,
            logging.WARNING,
            "upload_rollback",
            detail={"bundle_id": bundle_id, "reason": "exception"},
        )
        raise

    struct_log(
        logger,
        logging.INFO,
        "upload_stream_complete",
        detail={"bundle_id": bundle_id, "bytes_transferred": bytes_total},
    )

    return {"bundle_id": bundle_id, "ok": True}


async def _cassetta_put(path: str, content: str = "", files: str = "") -> str:
    """Store a file or bundle in the global store namespace. Overwrites are rejected.

    For single files, provide ``content``.
    For multi-file bundles, provide ``files`` as a JSON array of
    ``{"name": "...", "content": "..."}`` objects.
    Exactly one of ``content`` or ``files`` must be provided.
    """
    backend = _get_backend()
    config = _get_config()

    try:
        validate_path(path, allowed_chars=config.allowed_path_chars)
    except PathValidationError as e:
        raise ValueError(f"Invalid path: {e}") from e

    if path.startswith("inbox/") or path.startswith("store/"):
        raise ValueError(f"Invalid path: Reserved path prefix in {path!r}")

    has_content = bool(content)
    has_files = bool(files)
    if has_content and has_files:
        raise ValueError("Provide either 'content' or 'files', not both")
    if not has_content and not has_files:
        raise ValueError("Provide either 'content' or 'files'")

    if has_files:
        parsed = _parse_files_arg(files)
    else:
        parsed = [(path.split("/")[-1], content.encode("utf-8"))]

    total_size = sum(len(d) for _, d in parsed)
    try:
        await _evaluate_upload_or_raise(_build_upload_manifest(parsed))
    except LimitsRejection as exc:
        raise ValueError(str(exc)) from exc

    file_count = len(parsed)
    await _enforce(f"files:{path}", "write")

    bundle_path = f"{STORE_NAMESPACE}/{path}"
    bundle_id = _new_bundle_id()
    try:
        await backend.delete_bundle(bundle_path)
    except FileNotFoundError:
        pass
    try:
        await _write_bundle(
            backend,
            bundle_path,
            parsed,
            sender=None,
            content_type="application/octet-stream",
            bundle_id=bundle_id,
        )
    except BundlePathConflictError as exc:
        raise ValueError(str(exc)) from exc

    identity = get_current_identity()
    struct_log(
        logger,
        logging.INFO,
        "file.uploaded",
        identity_label=identity.label if identity else None,
        resource=f"files:{path}",
        action="put",
        result="ok",
        detail={"size": total_size, "via": "mcp", "file_count": file_count, "bundle_id": bundle_id},
    )
    _get_metrics().increment("cassetta.files.operations", tags={"action": "put"})
    _get_metrics().increment("cassetta.files.bytes", value=total_size, tags={"action": "put"})
    if file_count > 1:
        return f"Stored bundle '{path}' ({file_count} files, {total_size} bytes)"
    return f"Stored {path} ({total_size} bytes)"


async def _cassetta_get(path: str) -> str:
    """Retrieve the content of a file at the given path in the store namespace."""
    backend = _get_backend()
    config = _get_config()

    try:
        validate_path(path, allowed_chars=config.allowed_path_chars)
    except PathValidationError as e:
        raise ValueError(f"Invalid path: {e}") from e

    await _enforce(f"files:{path}", "read")

    bundle_path = f"{STORE_NAMESPACE}/{path}"
    try:
        meta = await backend.read_bundle_meta(bundle_path)
    except FileNotFoundError as exc:
        raise ValueError(f"File not found: {path}") from exc

    if _is_expired_by_meta(str(meta.get("created_at", "")), config.default_ttl):
        try:
            await backend.delete_bundle(bundle_path)
        except FileNotFoundError:
            pass
        raise ValueError(f"File not found: {path}")

    file_count = int(meta.get("file_count", 1))
    identity = get_current_identity() or Identity(label="dev", extra={"dev": True})
    struct_log(
        logger,
        logging.INFO,
        "file.downloaded",
        identity_label=identity.label,
        resource=f"files:{path}",
        action="get",
        result="ok",
        detail={"via": "mcp", "file_count": file_count, "bundle_id": meta.get("bundle_id")},
    )
    _get_metrics().increment("cassetta.files.operations", tags={"action": "get"})

    # Policy decides inline vs reference.
    policy = _get_limits_policy()
    records = list(meta.get("files", []))
    total_size = sum(int(f.get("size", 0)) for f in records)
    entry: DownloadEntry = {"file_count": file_count, "total_size": total_size}
    decision = await policy.evaluate_download(
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

    if mode == "reference":
        assert _reference_transport is not None, "MCP reference_transport not configured"
        ref_envelope = build_reference_payload_for_store(
            config=config,
            policy=policy,
            transport=_reference_transport,
            identity=identity,
            bundle_path=bundle_path,
            meta=meta,
            recipient=identity.label,
        )
        return json.dumps(ref_envelope)

    # Inline path — unified envelope across single + multi.
    payloads = await _collect_bundle_files(backend, bundle_path, records)
    _get_metrics().increment("cassetta.files.bytes", value=total_size, tags={"action": "get"})
    inline_envelope = build_inline_envelope(meta, payloads)
    return json.dumps(inline_envelope)


async def _cassetta_delete(path: str) -> str:
    """Delete a bundle at the given path in the store namespace."""
    backend = _get_backend()
    config = _get_config()

    try:
        validate_path(path, allowed_chars=config.allowed_path_chars)
    except PathValidationError as e:
        raise ValueError(f"Invalid path: {e}") from e

    await _enforce(f"files:{path}", "delete")

    bundle_path = f"{STORE_NAMESPACE}/{path}"
    try:
        await backend.delete_bundle(bundle_path)
    except FileNotFoundError as exc:
        raise ValueError(f"File not found: {path}") from exc

    identity = get_current_identity()
    struct_log(
        logger,
        logging.INFO,
        "file.deleted",
        identity_label=identity.label if identity else None,
        resource=f"files:{path}",
        action="delete",
        result="ok",
        detail={"via": "mcp"},
    )
    _get_metrics().increment("cassetta.files.operations", tags={"action": "delete"})
    return f"Deleted {path}"


async def _cassetta_list(prefix: str = "") -> str:
    """List bundles in the store namespace. Returns JSON with per-bundle manifests."""
    backend = _get_backend()
    config = _get_config()

    await _enforce("files:*", "list")

    scan_prefix = f"{STORE_NAMESPACE}/" + (prefix.lstrip("/") if prefix else "")
    refs = list(backend.list_bundles(scan_prefix))
    entries: list[dict[str, object]] = []
    for ref in refs:
        try:
            meta = await backend.read_bundle_meta(ref.path)
        except FileNotFoundError:
            continue
        created_at_iso = str(meta.get("created_at", ""))
        if _is_expired_by_meta(created_at_iso, config.default_ttl):
            continue
        display_path = ref.path.removeprefix(f"{STORE_NAMESPACE}/")
        entries.append(_listing_entry(display_path, meta, config.default_ttl, sender=False))

    if not entries:
        return "No files found."

    identity = get_current_identity()
    struct_log(
        logger,
        logging.INFO,
        "file.listed",
        identity_label=identity.label if identity else None,
        resource=f"files:{prefix}*",
        action="list",
        result="ok",
        detail={"count": len(entries), "via": "mcp"},
    )
    _get_metrics().increment("cassetta.files.operations", tags={"action": "list"})
    return json.dumps(entries, indent=2)


async def _cassetta_inbox(agent: str = "", prefix: str = "") -> str:
    """List bundles in an agent's inbox. Defaults to caller's own inbox."""
    backend = _get_backend()
    config = _get_config()

    target = agent or _sender_label.get() or ""
    if not target:
        raise ValueError("No agent specified and no sender identity available")

    await _enforce(f"inbox:{target}", "list")

    scan_prefix = f"{INBOX_NAMESPACE}/{target}/"
    if prefix:
        scan_prefix = f"{INBOX_NAMESPACE}/{target}/{prefix}"

    # Hide bundles with an active reference-mode claim.
    hidden: set[str] = set()
    if _claim_store is not None:
        policy = _get_limits_policy()
        identity = get_current_identity() or Identity(label="dev", extra={"dev": True})
        ttls = policy.ttls(PolicyContext(identity=identity))
        hidden = set(
            await _claim_store.iter_active_by_bundle_path(int(ttls["download_claim_ttl"])),
        )

    refs = list(backend.list_bundles(scan_prefix))
    entries: list[dict[str, object]] = []
    for ref in refs:
        if ref.path in hidden:
            continue
        try:
            meta = await backend.read_bundle_meta(ref.path)
        except FileNotFoundError:
            continue
        created_at_iso = str(meta.get("created_at", ""))
        if _is_expired_by_meta(created_at_iso, config.default_ttl):
            continue
        display_path = ref.path.removeprefix(f"{INBOX_NAMESPACE}/{target}/")
        entries.append(_listing_entry(display_path, meta, config.default_ttl, sender=True))

    entries.sort(key=lambda f: str(f["created_at"]), reverse=True)

    if not entries:
        return json.dumps([])

    listing_identity = get_current_identity()
    struct_log(
        logger,
        logging.INFO,
        "inbox.listed",
        identity_label=listing_identity.label if listing_identity else None,
        resource=f"inbox:{target}",
        action="list",
        result="ok",
        detail={"count": len(entries), "via": "mcp"},
    )
    _get_metrics().increment("cassetta.inbox.operations", tags={"action": "list"})
    return json.dumps(entries, indent=2)


async def _resolve_latest_inbox_bundle(
    backend: StorageBackend, target: str, ttl: int
) -> tuple[str, dict[str, Any]] | None:
    scan_prefix = f"{INBOX_NAMESPACE}/{target}/"
    newest_path: str | None = None
    newest_time = ""
    newest_meta: dict[str, Any] | None = None
    for ref in backend.list_bundles(scan_prefix):
        try:
            meta = await backend.read_bundle_meta(ref.path)
        except FileNotFoundError:
            continue
        created_at = str(meta.get("created_at", ""))
        if _is_expired_by_meta(created_at, ttl):
            continue
        if created_at > newest_time:
            newest_time = created_at
            newest_path = ref.path
            newest_meta = meta
    if newest_path is None or newest_meta is None:
        return None
    return newest_path, newest_meta


async def _cassetta_pick(path: str) -> str:
    """Get and delete a bundle from own inbox (atomic consume).

    Use path="latest" to pick the most recently received bundle.
    ``path`` otherwise refers to the bundle_id.
    """
    backend = _get_backend()
    config = _get_config()

    target = _sender_label.get() or ""
    if not target:
        raise ValueError("No sender identity available for inbox access")

    await _enforce(f"inbox:{target}", "pick")

    if path == "latest":
        resolved = await _resolve_latest_inbox_bundle(backend, target, config.default_ttl)
        if resolved is None:
            raise ValueError("No files in inbox")
        bundle_path, meta = resolved
    else:
        try:
            validate_path(path, allowed_chars=config.allowed_path_chars)
        except PathValidationError as e:
            raise ValueError(f"Invalid path: {e}") from e
        bundle_path = f"{INBOX_NAMESPACE}/{target}/{path}"
        try:
            meta = await backend.read_bundle_meta(bundle_path)
        except FileNotFoundError as exc:
            raise ValueError(f"File not found: {path}") from exc
        if _is_expired_by_meta(str(meta.get("created_at", "")), config.default_ttl):
            try:
                await backend.delete_bundle(bundle_path)
            except FileNotFoundError:
                pass
            raise ValueError(f"File not found: {path}")

    records = list(meta.get("files", []))
    file_count = int(meta.get("file_count", len(records)))
    total_size = sum(int(f.get("size", 0)) for f in records)

    identity = get_current_identity() or Identity(label="dev", extra={"dev": True})
    policy = _get_limits_policy()
    entry: DownloadEntry = {"file_count": file_count, "total_size": total_size}
    decision = await policy.evaluate_download(
        PolicyContext(identity=identity),
        entry,
    )
    mode = decision.get("mode")
    struct_log(
        logger,
        logging.DEBUG,
        "download_mode_decision",
        identity_label=identity.label,
        detail={"bundle_id": meta.get("bundle_id"), "mode": mode, "namespace": "inbox"},
    )

    if mode == "reference":
        # Ordering invariant — credential returned ONLY after
        # claim sidecar is fsync'd on disk. BundleClaimedError →
        # "File not found" (same surface as a missing bundle).
        assert _reference_transport is not None, "MCP reference_transport not configured"
        assert _claim_store is not None, "MCP claim_store not configured"
        try:
            ref_envelope = await build_reference_payload_for_inbox(
                config=config,
                policy=policy,
                transport=_reference_transport,
                claim_store=_claim_store,
                identity=identity,
                bundle_path=bundle_path,
                meta=meta,
                recipient=target,
                write_claim=True,
            )
        except BundleClaimedError as exc:
            raise ValueError(f"File not found: {path}") from exc

        struct_log(
            logger,
            logging.INFO,
            "inbox.picked",
            identity_label=identity.label,
            resource=f"inbox:{target}",
            action="pick",
            result="ok",
            detail={
                "path": path,
                "via": "mcp",
                "mode": "reference",
                "file_count": file_count,
                "bundle_id": meta.get("bundle_id"),
            },
        )
        _get_metrics().increment("cassetta.inbox.operations", tags={"action": "pick"})
        return json.dumps(ref_envelope)

    # Inline path — consume + delete, unified envelope.
    payloads = await _collect_bundle_files(backend, bundle_path, records)

    try:
        await backend.delete_bundle(bundle_path)
    except FileNotFoundError:
        pass

    struct_log(
        logger,
        logging.INFO,
        "inbox.picked",
        identity_label=identity.label,
        resource=f"inbox:{target}",
        action="pick",
        result="ok",
        detail={
            "path": path,
            "via": "mcp",
            "mode": "inline",
            "file_count": file_count,
            "bundle_id": meta.get("bundle_id"),
        },
    )
    _get_metrics().increment("cassetta.inbox.operations", tags={"action": "pick"})

    inline_envelope = build_inline_envelope(meta, payloads)
    return json.dumps(inline_envelope)


async def _cassetta_capabilities() -> str:
    """Return the server's advertised limits and features.

    Reports max inline size, per-file and per-bundle limits, credential TTLs,
    and supported encodings. Read-only and side-effect-free; cache the result
    per session to size a send before calling ``cassetta_send_init``.
    """
    policy = _get_limits_policy()
    identity = get_current_identity() or Identity(label="dev", extra={"dev": True})
    doc = await build_capabilities_document(
        policy,
        PolicyContext(identity=identity),
        via="mcp",
    )
    # Capability query counter (via=mcp).
    safe_emit(
        metric_name="cassetta.capabilities.queries",
        metric_tags={"via": "mcp"},
        metrics=_get_metrics(),
    )
    return json.dumps(doc)


async def _cassetta_peek(path: str) -> str:
    """Read bundle metadata without consuming the bundle.

    Accepts either a bare path (treated as ``store/``), an explicit
    ``store/...`` prefix, or an ``inbox/{recipient}/...`` prefix.
    """
    backend = _get_backend()
    config = _get_config()

    if path.startswith("inbox/"):
        remainder = path.removeprefix("inbox/")
        if "/" not in remainder:
            raise ValueError(f"Invalid path: {path}")
        agent, file_path = remainder.split("/", 1)
        try:
            validate_path(file_path, allowed_chars=config.allowed_path_chars)
        except PathValidationError as e:
            raise ValueError(f"Invalid path: {e}") from e
        bundle_path = path
        resource = f"inbox:{agent}"
        action_kind = "inbox"
    elif path.startswith("store/"):
        file_path = path.removeprefix("store/")
        try:
            validate_path(file_path, allowed_chars=config.allowed_path_chars)
        except PathValidationError as e:
            raise ValueError(f"Invalid path: {e}") from e
        bundle_path = path
        resource = f"files:{file_path}"
        action_kind = "files"
    else:
        try:
            validate_path(path, allowed_chars=config.allowed_path_chars)
        except PathValidationError as e:
            raise ValueError(f"Invalid path: {e}") from e
        bundle_path = f"{STORE_NAMESPACE}/{path}"
        resource = f"files:{path}"
        action_kind = "files"

    await _enforce(resource, "peek")

    identity = get_current_identity()
    identity_label = identity.label if identity else None

    try:
        meta = await backend.read_bundle_meta(bundle_path)
    except FileNotFoundError as exc:
        struct_log(
            logger,
            logging.INFO,
            "peek.not_found",
            identity_label=identity_label,
            resource=resource,
            detail={"path": path, "found": False, "reason": "absent"},
        )
        raise ValueError(f"Not found: {path}") from exc

    created_at_iso = str(meta.get("created_at", ""))
    if _is_expired_by_meta(created_at_iso, config.default_ttl):
        struct_log(
            logger,
            logging.INFO,
            "peek.not_found",
            identity_label=identity_label,
            resource=resource,
            detail={"path": path, "found": False, "reason": "expired"},
        )
        raise ValueError(f"Not found: {path}")

    file_count = int(meta.get("file_count", 0))
    total_size = sum(int(f.get("size", 0)) for f in meta.get("files", []))
    struct_log(
        logger,
        logging.DEBUG,
        "peek.ok",
        identity_label=identity_label,
        resource=resource,
        detail={
            "path": path,
            "found": True,
            "file_count": file_count,
            "total_size": total_size,
            "bundle_id": meta.get("bundle_id"),
        },
    )
    metric = "cassetta.inbox.operations" if action_kind == "inbox" else "cassetta.files.operations"
    _get_metrics().increment(metric, tags={"action": "peek"})

    return json.dumps({"bundle": meta})


async def _cassetta_broadcast(
    path: str,
    content: str = "",
    files: str = "",
    *,
    ctx: Context[Any, Any, Any] | None = None,
) -> str:
    """Send content to all visible recipients."""
    backend = _get_backend()
    config = _get_config()

    if _key_store is None:
        raise ValueError("Key store not configured")
    if _access_policy is None:
        raise ValueError("Access policy not configured")
    if _alias_resolver is None:
        raise ValueError("Alias resolver not configured")

    # Per-tool-call rate limiting. The MCP transport
    # multiplexes many tool calls inside one HTTP request, so the
    # decorator/middleware level cannot enforce per-call. Imperative
    # check shares the same `route=broadcast` bucket as REST.
    request = ctx.request_context.request if ctx is not None else None
    if request is not None and request.client is not None:
        try:
            check_rate_limit_imperative(request, config.rate_limit_broadcast, route="broadcast")
        except RateLimitExceeded as exc:
            _get_metrics().increment(
                "cassetta.rate_limit.hits",
                tags={"route": "broadcast", "reason": "rate"},
            )
            retry_after = 60
            try:
                retry_after = int(exc.limit.limit.get_expiry())  # type: ignore[union-attr]
            except Exception:
                pass
            raise ValueError(
                json.dumps({"error": "rate_limit", "retry_after": retry_after}),
            ) from exc

    try:
        validate_path(path, allowed_chars=config.allowed_path_chars)
    except PathValidationError as e:
        raise ValueError(f"Invalid path: {e}") from e

    has_content = bool(content)
    has_files = bool(files)
    if has_content and has_files:
        raise ValueError("Provide either 'content' or 'files', not both")
    if not has_content and not has_files:
        raise ValueError("Provide either 'content' or 'files'")

    sender = _sender_label.get()

    if has_files:
        parsed = _parse_files_arg(files)
    else:
        parsed = [(path.split("/")[-1], content.encode("utf-8"))]

    file_count = len(parsed)
    try:
        await _evaluate_upload_or_raise(_build_upload_manifest(parsed))
    except LimitsRejection as exc:
        raise ValueError(str(exc)) from exc

    identity = get_current_identity() or Identity(label="dev", extra={"dev": True})
    metrics = _get_metrics()

    keys = await _key_store.list_keys()
    candidate_labels = [k.label for k in keys if k.is_active and k.label != sender]

    # Fan-out cap rejection BEFORE any storage write.
    try:
        _check_fanout_cap(
            target_count=len(candidate_labels),
            max_targets=config.broadcast_max_targets,
        )
    except FanoutCapExceeded as exc:
        _get_metrics().increment(
            "cassetta.rate_limit.hits",
            tags={"route": "broadcast", "reason": "fanout_cap"},
        )
        raise ValueError(
            json.dumps(
                {
                    "error": "rate_limit",
                    "reason": "fanout_cap",
                    "max_targets": exc.max_targets,
                }
            )
        ) from exc

    visibility_failed = False
    try:
        visible_labels = await _access_policy.visible_agents(
            identity,
            candidate_labels,
        )
    except Exception:
        struct_log(
            logger,
            logging.ERROR,
            "broadcast.visibility_failed",
            identity_label=identity.label,
            detail={"path": path, "sender": sender, "via": "mcp"},
        )
        visible_labels = []
        visibility_failed = True

    delivered: list[str] = []
    denied_count = 0
    failed_count = 0

    for label in visible_labels:
        try:
            resolved = await _alias_resolver.resolve(label, sender_label=sender)
        except Exception:
            failed_count += 1
            continue
        if resolved is None:
            failed_count += 1
            continue

        try:
            allowed = await _access_policy.check(
                identity,
                f"inbox:{label}",
                "write",
            )
        except Exception:
            denied_count += 1
            continue
        if not allowed:
            denied_count += 1
            # policy_kind derived from access_policy.kind so cloud
            # team-policy denials are tagged policy_kind=cloud (field)
            # / =team (counter tag).
            _kind = getattr(_access_policy, "kind", "core")
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

        try:
            for target in resolved.inbox_targets:
                bundle_path = f"{target.rstrip('/')}/{path}"
                try:
                    await backend.delete_bundle(bundle_path)
                except FileNotFoundError:
                    pass
                await _write_bundle(
                    backend,
                    bundle_path,
                    parsed,
                    sender=sender,
                    content_type="application/octet-stream",
                    bundle_id=_new_bundle_id(),
                )
            delivered.append(label)
        except Exception:
            failed_count += 1

    if visibility_failed:
        result = "error"
    elif not visible_labels:
        result = "noop"
    elif delivered and (denied_count or failed_count):
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
            "denied": denied_count,
            "failed": failed_count,
            "via": "mcp",
            "file_count": file_count,
        },
        metric_name="cassetta.broadcast.operations",
        metric_tags={"result": result},
        metrics=metrics,
    )

    if not visible_labels:
        return f"Broadcast '{path}': no visible recipients"
    names = ", ".join(delivered)
    return f"Broadcast '{path}' to {len(delivered)} recipients ({denied_count} denied, {failed_count} failed): {names}"


async def _cassetta_agents() -> str:
    """List visible agents — labels filtered through AccessPolicy.visible_agents."""
    if _key_store is None:
        raise ValueError("Key store not configured")
    if _access_policy is None:
        raise ValueError("Access policy not configured")

    identity = get_current_identity() or Identity(label="dev", extra={"dev": True})
    keys = await _key_store.list_keys()
    active = [k for k in keys if k.is_active]
    labels = [k.label for k in active]
    try:
        visible_labels = await _access_policy.visible_agents(identity, labels)
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
    agents = [{"label": k.label, "created_at": k.created_at.isoformat()} for k in active if k.label in visible_set]
    result_tag = "unfiltered" if len(visible_labels) == len(labels) else "filtered"
    _get_metrics().increment(
        "cassetta.agents.list",
        tags={"result": result_tag},
    )
    return json.dumps(agents)


def create_mcp_server(allowed_hosts: tuple[str, ...] = ()) -> FastMCP:
    """Create a fresh FastMCP instance with all cassetta tools registered.

    Args:
        allowed_hosts: list of Host header values to accept (DNS-rebinding
            protection). When empty, only localhost is allowed (FastMCP default).
            Set this when the server is reachable via non-loopback hostnames.
    """
    transport_security = TransportSecuritySettings(
        allowed_hosts=list(allowed_hosts),
    )
    mcp = FastMCP(
        "Cassetta",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=transport_security,
    )
    # Advertise cassetta's own version in serverInfo. mcp 1.27.0's FastMCP has no
    # public `version` setter, so set it on the wrapped low-level server — the
    # value flows into the initialize handshake's serverInfo.
    mcp._mcp_server.version = __version__

    mcp.tool(name="cassetta_put")(_cassetta_put)
    mcp.tool(name="cassetta_get")(_cassetta_get)
    mcp.tool(name="cassetta_delete")(_cassetta_delete)
    mcp.tool(name="cassetta_list")(_cassetta_list)

    mcp.tool(name="cassetta_send_init")(_cassetta_send_init)
    mcp.tool(name="cassetta_send_inline")(_cassetta_send_inline)
    mcp.tool(name="cassetta_inbox")(_cassetta_inbox)
    mcp.tool(name="cassetta_pick")(_cassetta_pick)
    mcp.tool(name="cassetta_peek")(_cassetta_peek)
    mcp.tool(name="cassetta_capabilities")(_cassetta_capabilities)

    mcp.tool(name="cassetta_agents")(_cassetta_agents)
    mcp.tool(name="cassetta_broadcast")(_cassetta_broadcast)

    return mcp
