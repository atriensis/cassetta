"""Layer 3 core — filesystem-backed ``ClaimStorage`` implementation.

One JSON sidecar per active claim under ``<base>/{jti}.json`` plus a
bundle-path-keyed dotfile lock (``.{sha256(bundle_path)[:16]}.lock``)
that serializes concurrent ``issue()`` on the same bundle path
(FR-011a). Bodies are byte-identical to the Azure sibling so future
cross-backend migration tooling stays cheap.

Method bodies call blocking POSIX syscalls inline — ``os.open`` /
``os.fsync`` / ``os.replace`` — and are declared ``async def`` to
match the ``ClaimStorage`` Protocol without any ``await`` inside. The
sync POSIX syscalls take microseconds on local disk; offloading to a
thread pool would add overhead without buying real concurrency.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import ClassVar

from cassetta.claims import ClaimRecord, _iso_to_epoch
from cassetta.protocols.claim_storage import BundleClaimedError
from cassetta.structured_log import struct_log

logger = logging.getLogger("cassetta")

_SCHEMA_VERSION = 1


class FilesystemClaimStorage:
    """Filesystem-backed storage for :class:`ClaimRecord` sidecars.

    Keyed by ``jti``: one ``{jti}.json`` per record. Thread-safe only
    under the atomicity primitives the host OS provides for
    ``O_CREAT|O_EXCL`` and ``os.replace`` — no per-process locks.
    """

    kind: ClassVar[str] = "filesystem"

    def __init__(self, base_dir: Path) -> None:
        self._base = Path(base_dir)

    def _path(self, jti: str) -> Path:
        return self._base / f"{jti}.json"

    def _lock_path(self, bundle_path: str) -> Path:
        digest = hashlib.sha256(bundle_path.encode("utf-8")).hexdigest()[:16]
        return self._base / f".{digest}.lock"

    def _ensure_base(self) -> None:
        self._base.mkdir(parents=True, exist_ok=True)

    async def issue(self, claim: ClaimRecord) -> None:
        self._ensure_base()
        lock_path = self._lock_path(claim.bundle_path)
        try:
            lock_fd = os.open(
                lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
            )
        except FileExistsError:
            raise BundleClaimedError(claim.bundle_path) from None
        try:
            with os.fdopen(lock_fd, "wb", closefd=True) as f:
                f.write(claim.jti.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lock_path)
            raise

        path = self._path(claim.jti)
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
            )
        except FileExistsError:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lock_path)
            raise BundleClaimedError(claim.bundle_path) from None
        try:
            with os.fdopen(fd, "wb", closefd=True) as f:
                f.write(json.dumps(asdict(claim)).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lock_path)
            raise

    async def get(self, jti: str) -> ClaimRecord | None:
        try:
            body = self._path(jti).read_bytes()
        except FileNotFoundError:
            return None
        return self._parse(body)

    async def mark_fetched(self, jti: str, name: str) -> ClaimRecord:
        current = await self.get(jti)
        if current is None:
            raise FileNotFoundError(self._path(jti))
        if name in current.files_fetched:
            return current
        updated = replace(
            current, files_fetched=[*current.files_fetched, name],
        )
        self._durable_write(updated)
        return updated

    def _durable_write(self, claim: ClaimRecord) -> None:
        self._ensure_base()
        final = self._path(claim.jti)
        tmp = final.with_suffix(".json.tmp")
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
        )
        try:
            with os.fdopen(fd, "wb", closefd=True) as f:
                f.write(json.dumps(asdict(claim)).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    async def delete(self, jti: str) -> None:
        record: ClaimRecord | None = None
        try:
            record = await self.get(jti)
        except ValueError:
            record = None
        if record is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._lock_path(record.bundle_path))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._path(jti))

    async def iter_all(self) -> AsyncIterator[ClaimRecord]:
        try:
            entries = list(os.scandir(self._base))
        except FileNotFoundError:
            return
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            if entry.name.endswith(".json.tmp"):
                continue
            try:
                body = Path(entry.path).read_bytes()
                yield self._parse(body)
            except (ValueError, OSError) as exc:
                struct_log(
                    logger,
                    logging.WARNING,
                    "claim_sidecar_malformed",
                    detail={"path": entry.path, "reason": str(exc)},
                )

    async def iter_active_by_bundle_path(
        self, ttl_s: int,
    ) -> Mapping[str, str]:
        now = time.time()
        active: dict[str, str] = {}
        async for claim in self.iter_all():
            try:
                age = now - _iso_to_epoch(claim.created_at)
            except ValueError:
                continue
            if age >= ttl_s:
                continue
            active[claim.bundle_path] = claim.jti
        return active

    @staticmethod
    def _parse(body: bytes) -> ClaimRecord:
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed claim sidecar: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("claim sidecar body is not an object")
        version = data.get("schema_version")
        if version != _SCHEMA_VERSION:
            raise ValueError(
                f"unknown claim schema_version: {version!r}",
            )
        return ClaimRecord(
            schema_version=int(version),
            jti=str(data["jti"]),
            bundle_path=str(data["bundle_path"]),
            bundle_id=str(data["bundle_id"]),
            recipient=str(data["recipient"]),
            created_at=str(data["created_at"]),
            files_fetched=list(data.get("files_fetched", [])),
        )
