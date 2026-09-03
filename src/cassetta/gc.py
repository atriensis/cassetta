"""Passive garbage collection for orphan bundle directories.

Single-pass :func:`sweep` over ``inbox/`` and ``store/`` namespaces issued
on the ``CoreLimitsPolicy.ttls().passive_gc_interval`` cadence by
:func:`schedule_reaper`. Orphans (bundle directories without a
``meta.json`` sidecar) older than ``passive_gc_min_age`` seconds are
deleted; committed bundles are left alone.

:func:`sweep_claims` runs alongside :func:`sweep` — expired
claim sidecars complete their bundle deletion (inbox) or simply vanish
(incomplete claims release the bundle back to the listing).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from cassetta.structured_log import struct_log

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fastapi import FastAPI

    from cassetta.protocols.claim_storage import ClaimStorage
    from cassetta.protocols.limits import LimitsPolicy
    from cassetta.protocols.storage import BundleRef

logger = logging.getLogger("cassetta")

_NAMESPACES: tuple[str, ...] = ("inbox/", "store/")


class _ReaperBackend(Protocol):
    """Structural subset of ``StorageBackend`` used by the reaper."""

    def list_bundles(
        self,
        prefix: str,
        *,
        include_orphans: bool = False,
    ) -> "Iterable[BundleRef]": ...

    async def delete_bundle(self, path: str) -> None: ...

    async def read_bundle_meta(self, path: str) -> dict[str, Any]: ...


async def sweep(storage: _ReaperBackend, *, min_age_s: int) -> None:
    """Run one reaper pass over ``inbox/`` and ``store/``.

    For each namespace, list bundles including orphans; delete any orphan
    whose ``mtime`` is older than ``min_age_s`` seconds. Committed bundles
    are skipped.
    """
    now = time.time()
    for prefix in _NAMESPACES:
        try:
            refs = list(storage.list_bundles(prefix, include_orphans=True))
        except Exception:
            struct_log(
                logger,
                logging.ERROR,
                "gc_list_failed",
                detail={"prefix": prefix},
            )
            continue
        for ref in refs:
            if ref.has_meta:
                continue
            age = now - ref.mtime
            if age < min_age_s:
                continue
            try:
                await storage.delete_bundle(ref.path)
            except FileNotFoundError:
                # Race with another reaper or manual cleanup — benign.
                continue
            except Exception:
                struct_log(
                    logger,
                    logging.WARNING,
                    "gc_delete_failed",
                    detail={"path": ref.path},
                )
                continue
            struct_log(
                logger,
                logging.INFO,
                "gc_reaped",
                detail={"path": ref.path, "age_seconds": int(age)},
            )


def _iso_to_epoch(iso: str) -> float:
    """Parse an ISO-8601 UTC timestamp into epoch seconds."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def sweep_claims(
    claim_store: "ClaimStorage",
    storage: _ReaperBackend,
    *,
    ttl_s: int,
) -> None:
    """Drop expired claim sidecars; on completion, delete the inbox bundle.

    Per the reaper contract in ``contracts/claim-sidecar.md``:

    - ``age < ttl_s``: skip.
    - ``age >= ttl_s`` + all manifest names in ``files_fetched`` +
      bundle is under ``inbox/`` → ``storage.delete_bundle(bundle_path)``
      + ``claim_store.delete(jti)``.
    - ``age >= ttl_s`` + completion incomplete → ``claim_store.delete(jti)``
      only (bundle reappears in listings next cycle).
    - Malformed / unknown-schema sidecars are handled by ``iter_all``
      (WARN log + skip); unreadable files are left for the next sweep.
    """
    claims: list[Any] = []
    try:
        async for claim in claim_store.iter_all():
            claims.append(claim)
    except Exception:
        struct_log(
            logger,
            logging.ERROR,
            "gc_claims_list_failed",
        )
        return

    now = time.time()
    for claim in claims:
        try:
            age = now - _iso_to_epoch(claim.created_at)
        except ValueError:
            # Malformed created_at — drop the sidecar defensively.
            await claim_store.delete(claim.jti)
            continue
        if age < ttl_s:
            continue

        # Expired. Is it complete + inbox?
        try:
            meta = await storage.read_bundle_meta(claim.bundle_path)
        except FileNotFoundError:
            # Bundle is gone; drop the sidecar.
            await claim_store.delete(claim.jti)
            struct_log(
                logger,
                logging.INFO,
                "download_claim_expired",
                detail={
                    "jti": claim.jti,
                    "bundle_path": claim.bundle_path,
                    "reason": "bundle_missing",
                },
            )
            continue
        except Exception:
            struct_log(
                logger,
                logging.WARNING,
                "gc_claims_read_meta_failed",
                detail={"jti": claim.jti, "bundle_path": claim.bundle_path},
            )
            continue

        manifest_names = {str(f.get("name")) for f in meta.get("files", [])}
        fetched = set(claim.files_fetched)
        complete = bool(manifest_names) and manifest_names.issubset(fetched)
        is_inbox = claim.bundle_path.startswith("inbox/")

        if complete and is_inbox:
            total_bytes = sum(int(f.get("size", 0)) for f in meta.get("files", []))
            try:
                await storage.delete_bundle(claim.bundle_path)
            except FileNotFoundError:
                pass
            except Exception:
                struct_log(
                    logger,
                    logging.WARNING,
                    "gc_claims_delete_bundle_failed",
                    detail={"jti": claim.jti, "bundle_path": claim.bundle_path},
                )
                # Still drop the sidecar — we can't recover the bundle.
            await claim_store.delete(claim.jti)
            struct_log(
                logger,
                logging.INFO,
                "download_claim_completed",
                detail={
                    "jti": claim.jti,
                    "bundle_id": claim.bundle_id,
                    "total_bytes": total_bytes,
                    "namespace": "inbox",
                    "via": "reaper",
                },
            )
        else:
            # Incomplete — release the bundle by dropping the claim only.
            await claim_store.delete(claim.jti)
            struct_log(
                logger,
                logging.INFO,
                "download_claim_expired",
                detail={
                    "jti": claim.jti,
                    "bundle_path": claim.bundle_path,
                    "reason": "incomplete",
                    "fetched": sorted(fetched),
                    "expected": sorted(manifest_names),
                },
            )


async def _reaper_loop(
    storage: _ReaperBackend,
    *,
    interval_s: int,
    min_age_s: int,
    claim_store: "ClaimStorage | None" = None,
    download_claim_ttl_s: int = 300,
) -> None:
    """Run :func:`sweep` (+ optional :func:`sweep_claims`) every ``interval_s``."""
    struct_log(
        logger,
        logging.INFO,
        "gc_scheduled",
        detail={"interval_s": interval_s, "min_age_s": min_age_s, "with_claims": claim_store is not None},
    )
    while True:
        try:
            await asyncio.sleep(interval_s)
            await sweep(storage, min_age_s=min_age_s)
            if claim_store is not None:
                await sweep_claims(
                    claim_store,
                    storage,
                    ttl_s=download_claim_ttl_s,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            struct_log(
                logger,
                logging.ERROR,
                "gc_loop_error",
                detail={"interval_s": interval_s},
            )


def schedule_reaper(
    app: "FastAPI",
    storage: _ReaperBackend,
    policy: "LimitsPolicy",
) -> asyncio.Task[None]:
    """Spawn the reaper task and stash it on ``app.state`` for cancellation."""
    from cassetta.protocols.identity import Identity
    from cassetta.protocols.limits import PolicyContext

    ttls = policy.ttls(PolicyContext(identity=Identity(label="gc", extra={})))
    claim_store = app.state.backends.claim_store
    task = asyncio.create_task(
        _reaper_loop(
            storage,
            interval_s=ttls["passive_gc_interval"],
            min_age_s=ttls["passive_gc_min_age"],
            claim_store=claim_store,
            download_claim_ttl_s=ttls["download_claim_ttl"],
        ),
        name="cassetta-gc-reaper",
    )
    app.state.gc_task = task
    return task
