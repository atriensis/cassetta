import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from cassetta import __version__
from cassetta import gc as _gc
from cassetta.auth.jwt_hot_reload import init_jwt_hot_reload, init_jwt_key_slots
from cassetta.config import AppConfig, load_config
from cassetta.defaults.default_limits import LimitsRejection
from cassetta.defaults.factory import BackendConfig, build_core_defaults
from cassetta.mcp_auth import MCPAuthMiddleware
from cassetta.mcp_server import configure as configure_mcp
from cassetta.mcp_server import create_mcp_server
from cassetta.middleware import RequestIdMiddleware
from cassetta.models import LimitsRejectionBody
from cassetta.protocols.metrics import MetricsProvider
from cassetta.protocols.storage import BundlePathConflictError, StorageBackend
from cassetta.rate_limit.limiter import (
    FanoutCapExceeded,
    RateLimitExceeded,
    _record_rate_limit_hit,
    limiter,
)
from cassetta.structured_log import configure_logging, safe_emit, struct_log

logger = logging.getLogger("cassetta")

_BUNDLE_NAMESPACES = ("store/", "inbox/")


def _retry_after_seconds(exc: RateLimitExceeded) -> int:
    """Best-effort window-in-seconds extraction from slowapi's exception.

    slowapi attaches the parsed ``Limit`` (which carries the underlying
    ``RateLimitItem``) on ``exc.limit``; its ``get_expiry`` reports the
    bucket's window length. Falls back to 60s if the structure changes.
    """
    try:
        limit_item = getattr(exc.limit, "limit", None)
        if limit_item is not None:
            return int(limit_item.get_expiry())
    except Exception:
        pass
    return 60


async def _lease_renewal_task(
    backend: StorageBackend,
    key: str,
    lease_id: str,
    interval: float = 30.0,
) -> None:
    """Periodically renew a lease to prevent expiry during long sweeps."""
    while True:
        await asyncio.sleep(interval)
        renewed = await backend.renew_lease(key, lease_id)
        if renewed:
            struct_log(logger, logging.DEBUG, "ttl.lease_renewed", detail={"key": key})
        else:
            struct_log(logger, logging.WARNING, "ttl.lease_renewal_failed", detail={"key": key})
            break


async def _ttl_cleanup_loop(
    backend: StorageBackend,
    config: AppConfig,
    metrics: MetricsProvider | None = None,
) -> None:
    """Periodically remove expired bundles across store/ and inbox/ namespaces.

    Uses lease-based coordination so that only one pod in a multi-pod
    deployment runs the cleanup sweep at a time.
    """
    interval = 60
    lease_key = "ttl-cleanup"
    lease_ttl = 60

    while True:
        await asyncio.sleep(interval)
        if config.default_ttl <= 0:
            continue

        lease_id = await backend.acquire_lease(lease_key, ttl_seconds=lease_ttl)
        if lease_id is None:
            struct_log(logger, logging.DEBUG, "ttl.lease_skipped", detail={"reason": "held by another pod"})
            continue

        struct_log(logger, logging.INFO, "ttl.sweep_started")

        renewal = asyncio.create_task(_lease_renewal_task(backend, lease_key, lease_id))

        deleted_count = 0
        try:
            now = time.time()
            for prefix in _BUNDLE_NAMESPACES:
                for ref in backend.list_bundles(prefix, include_orphans=False):
                    try:
                        meta = await backend.read_bundle_meta(ref.path)
                    except FileNotFoundError:
                        continue
                    created_iso = str(meta.get("created_at", ""))
                    try:
                        created = datetime.fromisoformat(created_iso)
                    except ValueError:
                        continue
                    if now - created.timestamp() > config.default_ttl:
                        try:
                            await backend.delete_bundle(ref.path)
                            deleted_count += 1
                            struct_log(
                                logger, logging.INFO, "ttl.file_deleted", resource=f"bundle:{ref.path}", result="ok"
                            )
                        except FileNotFoundError:
                            continue
                        except Exception:
                            struct_log(
                                logger, logging.ERROR, "ttl.file_error", resource=f"bundle:{ref.path}", result="error"
                            )
        except Exception:
            struct_log(logger, logging.ERROR, "ttl.sweep_error", result="error")
        finally:
            renewal.cancel()
            try:
                await renewal
            except asyncio.CancelledError:
                pass
            if metrics is not None and deleted_count > 0:
                safe_emit(
                    metric_name="cassetta.ttl.cleanup.files_deleted",
                    metric_value=deleted_count,
                    metrics=metrics,
                )

            struct_log(logger, logging.INFO, "ttl.cleanup", result="ok", detail={"files_deleted": deleted_count})

            released = await backend.release_lease(lease_key, lease_id)
            if released:
                struct_log(logger, logging.INFO, "ttl.lease_released")
            else:
                struct_log(logger, logging.WARNING, "ttl.lease_release_failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: initialise backends, run MCP + TTL + GC loops."""
    config: AppConfig = app.state.config
    backends: BackendConfig = app.state.backends

    struct_log(
        logger,
        logging.INFO,
        "config_loaded",
        detail={
            "primary_key_source": config.jwt_primary_key_source,
            "secondary_key": config.jwt_secondary_key is not None,
            "public_base_url": config.public_base_url,
            "dev_mode": app.state.dev_mode,
            "rate_limit_onboard": config.rate_limit_onboard,
            "rate_limit_broadcast": config.rate_limit_broadcast,
            "broadcast_max_targets": config.broadcast_max_targets,
            "jwt_key_overlap_ttl": config.jwt_key_overlap_ttl,
        },
    )
    # JWT primary-key hot-reload + boot-time validation. Order
    # matters — the validation warning must surface immediately after
    # ``config_loaded`` so dashboards see them in operator-natural order.
    init_jwt_hot_reload(app)
    if app.state.dev_mode:
        struct_log(logger, logging.WARNING, "dev_mode_enabled")

    backend = backends.backend
    if hasattr(backend, "initialize"):
        await backend.initialize()

    key_store = backends.key_store
    if hasattr(key_store, "initialize"):
        await key_store.initialize()

    # claim_storage_backend emission via safe_emit so the record parses as
    # JSON under CASSETTA_LOG_FORMAT=json. This is the single canonical
    # emission — a duplicate on the downstream side was removed.
    safe_emit(
        logger,
        logging.INFO,
        "claim_storage_backend",
        detail={"kind": backends.claim_store.kind},
    )

    configure_mcp(config, backends, jwt_keys=app.state.jwt_keys)

    mcp_server = app.state.mcp_server
    async with mcp_server.session_manager.run():
        cleanup_task: asyncio.Task[None] | None = None
        if config.default_ttl > 0:
            cleanup_task = asyncio.create_task(
                _ttl_cleanup_loop(
                    backend,
                    config,
                    metrics=backends.metrics_provider,
                )
            )

        gc_task: asyncio.Task[None] = _gc.schedule_reaper(
            app,
            backend,
            backends.limits_policy,
        )

        yield

        gc_task.cancel()
        try:
            await gc_task
        except asyncio.CancelledError:
            pass
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        if hasattr(backend, "close"):
            await backend.close()


def create_app(
    config: AppConfig | None = None,
    *,
    backends: BackendConfig | None = None,
    extra_log_trees: Sequence[str] = (),
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config: application configuration; loaded from the environment when
            omitted.
        backends: ready-made backend implementations; the core defaults are
            built from ``config`` when omitted.
        extra_log_trees: names of additional logger trees to route through the
            configured handler, forwarded verbatim to
            :func:`cassetta.structured_log.configure_logging`. An application
            embedding this server passes its own tree here so one process
            produces one log stream. Empty by default.
    """
    if config is None:
        config = load_config()

    configure_logging(config.log_format, extra_log_trees)

    if backends is None:
        backends = build_core_defaults(config)

    app = FastAPI(title="Cassetta", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.backends = backends
    app.state.dev_mode = config.dev_mode
    app.state.setup_token = config.setup_token
    # Shared limiter instance + decorator-friendly state hook.
    # slowapi looks for ``app.state.limiter`` when the decorator runs.
    app.state.limiter = limiter
    # Populate the runtime JWT key holder so verify-call
    # sites work even when the test client bypasses lifespan.
    init_jwt_key_slots(app)

    @app.exception_handler(BundlePathConflictError)
    async def _bundle_conflict_handler(_request: Request, exc: BundlePathConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "bundle_path_conflict",
                "conflicting_path": exc.conflicting_path,
                "kind": exc.kind,
            },
        )

    @app.exception_handler(LimitsRejection)
    async def _limits_rejection_handler(
        _request: Request,
        exc: LimitsRejection,
    ) -> JSONResponse:
        if exc.error == "cap_exceeded":
            status_code = status.HTTP_413_CONTENT_TOO_LARGE
        else:
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        return JSONResponse(
            status_code=status_code,
            content=LimitsRejectionBody(
                error=exc.error,
                constraint=exc.constraint,
                limit=exc.limit,
                observed=exc.observed,
            ).model_dump(),
        )

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(
        request: Request,
        exc: RateLimitExceeded,
    ) -> JSONResponse:
        # slowapi exposes the parsed Limit on `exc.limit`; we only need
        # the per-bucket window in seconds for Retry-After.
        retry_after = _retry_after_seconds(exc)
        path = request.url.path
        if path.startswith("/onboard"):
            route: str = "onboard"
        else:
            # Decorators only sit on /broadcast and /onboard today; default
            # to broadcast so misconfiguration still emits *a* counter
            # rather than silently dropping the increment.
            route = "broadcast"
        _record_rate_limit_hit(request, route=route, reason="rate")  # type: ignore[arg-type]
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": "rate_limit", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after)},
        )

    @app.exception_handler(FanoutCapExceeded)
    async def _fanout_cap_handler(
        request: Request,
        exc: FanoutCapExceeded,
    ) -> JSONResponse:
        _record_rate_limit_hit(request, route="broadcast", reason="fanout_cap")
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "rate_limit",
                "reason": "fanout_cap",
                "max_targets": exc.max_targets,
            },
            headers={"Retry-After": "0"},
        )

    from cassetta.routes.agents import router as agents_router
    from cassetta.routes.capabilities import router as capabilities_router
    from cassetta.routes.download import (
        DownloadError,
        download_error_handler,
    )
    from cassetta.routes.download import (
        router as download_router,
    )
    from cassetta.routes.files import router as files_router
    from cassetta.routes.inbox import router as inbox_router
    from cassetta.routes.keys import router as keys_router
    from cassetta.routes.upload import router as upload_router
    from cassetta.routes.uploads import router as uploads_router

    app.add_exception_handler(DownloadError, download_error_handler)  # type: ignore[arg-type]

    app.include_router(agents_router)
    app.include_router(capabilities_router)
    app.include_router(download_router)
    app.include_router(files_router)
    app.include_router(inbox_router)
    app.include_router(keys_router)
    app.include_router(upload_router)
    app.include_router(uploads_router)

    mcp_server = create_mcp_server(allowed_hosts=config.mcp_allowed_hosts)
    app.state.mcp_server = mcp_server
    mcp_asgi = mcp_server.streamable_http_app()
    app.mount("/mcp", MCPAuthMiddleware(mcp_asgi, fastapi_app=app))

    @app.get("/health")
    async def health() -> dict[str, str | bool]:
        key_store = app.state.backends.key_store
        if hasattr(key_store, "is_healthy") and not key_store.is_healthy():
            from fastapi.responses import JSONResponse

            return JSONResponse(  # type: ignore[return-value]
                status_code=503,
                content={
                    "status": "unhealthy",
                    "reason": "key_store_unreachable",
                    "dev_mode": app.state.dev_mode,
                },
            )
        return {"status": "ok", "dev_mode": app.state.dev_mode}

    app.add_middleware(RequestIdMiddleware)

    return app
