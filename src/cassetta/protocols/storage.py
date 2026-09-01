from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, BinaryIO, ClassVar, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class BundleRef:
    """Lightweight pointer to a bundle directory, returned by list_bundles."""

    path: str
    has_meta: bool
    mtime: float


class BundleWriter(Protocol):
    """Per-bundle write handle. Not thread-safe; one writer per bundle."""

    async def write_file(self, name: str, source: BinaryIO) -> int: ...

    async def commit(self, meta: dict[str, Any]) -> None: ...

    async def abort(self) -> None: ...


class BundlePathConflictError(Exception):
    """Raised by open_bundle_write when the target path shadows an existing bundle."""

    def __init__(
        self,
        conflicting_path: str,
        kind: Literal["shadow_parent", "shadow_child"],
    ) -> None:
        super().__init__(f"bundle_path_conflict at {conflicting_path} ({kind})")
        self.conflicting_path = conflicting_path
        self.kind = kind


@runtime_checkable
class StorageBackend(Protocol):
    kind: ClassVar[str]

    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def list(self, prefix: str = "") -> list[str]: ...

    async def exists(self, key: str) -> bool: ...

    async def get_creation_time(self, key: str) -> float: ...

    async def get_size(self, key: str) -> int: ...

    async def acquire_lease(self, key: str, ttl_seconds: int = 60) -> str | None: ...

    async def renew_lease(self, key: str, lease_id: str) -> bool: ...

    async def release_lease(self, key: str, lease_id: str) -> bool: ...

    async def open_bundle_write(self, path: str) -> BundleWriter: ...

    async def read_bundle_meta(self, path: str) -> dict[str, Any]: ...

    async def open_bundle_file_read(self, path: str, name: str) -> BinaryIO: ...

    def list_bundles(
        self,
        prefix: str,
        *,
        include_orphans: bool = False,
    ) -> Iterable[BundleRef]: ...

    async def delete_bundle(self, path: str) -> None: ...
