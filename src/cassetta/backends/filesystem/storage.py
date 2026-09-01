import fcntl
import json
import logging
import os
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, Literal

from cassetta.protocols.storage import BundlePathConflictError, BundleRef
from cassetta.structured_log import struct_log

logger = logging.getLogger("cassetta.storage.filesystem")


@dataclass
class _LeaseInfo:
    fd: int
    lease_id: str


DATA_PREFIX = "data/"
LOCKS_PREFIX = "locks/"
_BUNDLE_MANIFEST = "meta.json"
_BUNDLE_MANIFEST_TMP = ".meta.json.tmp"
_COPY_CHUNK = 64 * 1024


class FilesystemBackend:
    """Local filesystem storage backend.

    Stores files under a configurable root directory with namespace isolation:
    - ``data/`` for user files (both /files/ and /inbox/)
    - ``keys/`` for API key store
    - ``locks/`` for lease lock files

    User-facing operations automatically map to the ``data/`` namespace.
    """

    kind: ClassVar[str] = "filesystem"

    def __init__(self, root_path: str) -> None:
        self._root = Path(root_path)
        self._root.mkdir(parents=True, exist_ok=True)
        self._locks_dir = self._root / LOCKS_PREFIX
        self._leases: dict[str, _LeaseInfo] = {}

    def _resolve(self, key: str) -> Path:
        return self._root / DATA_PREFIX / key

    def _data_root(self) -> Path:
        return self._root / DATA_PREFIX

    async def put(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {key}")
        return path.read_bytes()

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {key}")
        path.unlink()

    async def list(self, prefix: str = "") -> list[str]:
        data_dir = self._data_root()
        if not data_dir.is_dir():
            return []
        results: list[str] = []
        for root, _dirs, files in os.walk(data_dir):
            for fname in files:
                full_path = Path(root) / fname
                relative = str(full_path.relative_to(data_dir))
                if prefix and not relative.startswith(prefix):
                    continue
                results.append(relative)
        return sorted(results)

    async def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    async def get_creation_time(self, key: str) -> float:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {key}")
        stat = path.stat()
        return stat.st_mtime

    async def get_size(self, key: str) -> int:
        path = self._resolve(key)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {key}")
        return path.stat().st_size

    async def acquire_lease(self, key: str, ttl_seconds: int = 60) -> str | None:
        if key in self._leases:
            return None

        self._locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._locks_dir / key

        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        except OSError:
            struct_log(logger, logging.ERROR, "lease.open_failed", detail={"key": key})
            return None

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None

        lease_id = uuid.uuid4().hex
        self._leases[key] = _LeaseInfo(fd=fd, lease_id=lease_id)
        struct_log(logger, logging.INFO, "lease.acquired", detail={"key": key, "lease_id": lease_id})
        return lease_id

    async def renew_lease(self, key: str, lease_id: str) -> bool:
        info = self._leases.get(key)
        if info is None or info.lease_id != lease_id:
            return False
        struct_log(logger, logging.DEBUG, "lease.renewed", detail={"key": key, "lease_id": lease_id})
        return True

    async def release_lease(self, key: str, lease_id: str) -> bool:
        info = self._leases.get(key)
        if info is None or info.lease_id != lease_id:
            return False
        try:
            fcntl.flock(info.fd, fcntl.LOCK_UN)
            os.close(info.fd)
        except OSError:
            struct_log(logger, logging.ERROR, "lease.release_error", detail={"key": key})
        del self._leases[key]
        struct_log(logger, logging.INFO, "lease.released", detail={"key": key, "lease_id": lease_id})
        return True

    # ---- Bundle operations (brief 512) ------------------------------------

    async def open_bundle_write(self, path: str) -> "FilesystemBundleWriter":
        bundle_dir = self._resolve(path)
        self._check_bundle_path_collision(bundle_dir)
        bundle_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            bundle_dir.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise BundlePathConflictError(path, _collision_kind(bundle_dir)) from exc
        return FilesystemBundleWriter(bundle_dir)

    async def read_bundle_meta(self, path: str) -> dict[str, Any]:
        bundle_dir = self._resolve(path)
        meta_path = bundle_dir / _BUNDLE_MANIFEST
        if not meta_path.is_file():
            raise FileNotFoundError(f"Bundle not found: {path}")
        try:
            data = json.loads(meta_path.read_text())
        except json.JSONDecodeError as exc:
            raise FileNotFoundError(f"Bundle meta unreadable: {path}") from exc
        if not isinstance(data, dict):
            raise FileNotFoundError(f"Bundle meta invalid: {path}")
        return data

    async def open_bundle_file_read(self, path: str, name: str) -> BinaryIO:
        bundle_dir = self._resolve(path)
        meta_path = bundle_dir / _BUNDLE_MANIFEST
        if not meta_path.is_file():
            raise FileNotFoundError(f"Bundle not found: {path}")
        inner = bundle_dir / name
        if not inner.is_file():
            raise FileNotFoundError(f"File not found: {path}/{name}")
        return inner.open("rb")

    def list_bundles(
        self,
        prefix: str,
        *,
        include_orphans: bool = False,
    ) -> Iterable[BundleRef]:
        """Recursively enumerate bundle directories under ``prefix``.

        A bundle directory is any directory that either (a) contains a
        ``meta.json`` manifest, or (b) contains files directly and no
        ``meta.json`` — the latter is an orphan from a crashed write and is
        included only when ``include_orphans`` is True.
        Traversal does not descend into bundle directories; once a directory
        is classified as a bundle (committed or orphan), its children are not
        treated as separate bundles.
        """
        data_dir = self._data_root()
        base_dir = data_dir / prefix if prefix else data_dir
        if not base_dir.is_dir():
            return iter(())
        results: list[BundleRef] = []
        for root, dirs, files in os.walk(base_dir):
            root_path = Path(root)
            if root_path == base_dir:
                # The prefix root itself is never a bundle — skip to children.
                # But still warn on stray files at the root.
                for fname in files:
                    rel = (root_path / fname).relative_to(data_dir).as_posix()
                    struct_log(
                        logger,
                        logging.WARNING,
                        "bundles.stray_entry",
                        detail={"path": rel, "prefix": prefix},
                    )
                continue
            rel = root_path.relative_to(data_dir).as_posix()
            has_meta = _BUNDLE_MANIFEST in files
            has_files = any(fname != _BUNDLE_MANIFEST for fname in files)
            if has_meta:
                results.append(BundleRef(path=rel, has_meta=True, mtime=root_path.stat().st_mtime))
                dirs[:] = []  # do not recurse into bundle contents
            elif has_files:
                if include_orphans:
                    results.append(BundleRef(path=rel, has_meta=False, mtime=root_path.stat().st_mtime))
                dirs[:] = []
        results.sort(key=lambda r: r.path)
        return iter(results)

    async def delete_bundle(self, path: str) -> None:
        bundle_dir = self._resolve(path)
        if not bundle_dir.is_dir():
            raise FileNotFoundError(f"Bundle not found: {path}")
        shutil.rmtree(bundle_dir)

    # ---- Collision detection helpers (bundle namespace) -------------------

    def _check_bundle_path_collision(self, bundle_dir: Path) -> None:
        """Detect shadow-parent/shadow-child collisions before mkdir.

        Ancestor traversal stops at the namespace root (``data/store`` or
        ``data/inbox/{recipient}``) — those directories are prefixes, not
        bundles, even if they contain stray files (which are logged via
        ``list_bundles`` and excluded from user-facing listings).
        """
        data_root = self._data_root()
        try:
            rel_parts = bundle_dir.relative_to(data_root).parts
        except ValueError:
            return
        if not rel_parts:
            return
        namespace_depth = 2 if rel_parts[0] == "inbox" else 1
        # If the bundle sits at (or above) the namespace level, nothing to
        # walk — collisions are handled by mkdir(exist_ok=False) at commit.
        if len(rel_parts) <= namespace_depth:
            return
        namespace_root = data_root
        for part in rel_parts[:namespace_depth]:
            namespace_root = namespace_root / part

        ancestor = bundle_dir.parent
        while ancestor != namespace_root:
            if ancestor == data_root:
                break
            if ancestor.is_dir() and _looks_like_bundle_dir(ancestor):
                rel = ancestor.relative_to(data_root).as_posix()
                raise BundlePathConflictError(rel, "shadow_child")
            ancestor = ancestor.parent

        if bundle_dir.is_dir():
            descendant = _find_descendant_bundle(bundle_dir)
            if descendant is not None:
                rel = descendant.relative_to(data_root).as_posix()
                raise BundlePathConflictError(rel, "shadow_parent")


class FilesystemBundleWriter:
    """Per-bundle write handle on the local filesystem (brief 512)."""

    def __init__(self, bundle_dir: Path) -> None:
        self._bundle_dir = bundle_dir
        self._written: dict[str, int] = {}
        self._committed = False

    async def write_file(self, name: str, source: BinaryIO) -> int:
        if not name:
            raise ValueError("Bundle file name must not be empty")
        if name in self._written:
            raise ValueError(f"Duplicate file name in bundle: {name}")
        target = self._bundle_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with target.open("wb") as dst:
            while True:
                chunk = source.read(_COPY_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                written += len(chunk)
        self._written[name] = written
        return written

    async def commit(self, meta: dict[str, Any]) -> None:
        files = meta.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("Bundle must contain at least one file")
        declared_names = [f.get("name") for f in files]
        if len(declared_names) != len(set(declared_names)):
            raise ValueError("Duplicate file names in bundle")

        # Verify every declared file was written with the declared size.
        written_names = set(self._written.keys())
        declared_set = set(declared_names)
        if written_names != declared_set:
            missing = declared_set - written_names
            extra = written_names - declared_set
            raise ValueError(f"Bundle file set mismatch (missing={sorted(missing)}, extra={sorted(extra)})")
        for entry in files:
            n = entry["name"]
            declared_size = entry.get("size")
            if not isinstance(declared_size, int):
                raise ValueError(f"files[].size must be an integer (got {type(declared_size).__name__})")
            if declared_size != self._written[n]:
                raise ValueError(f"Bundle size mismatch for {n}: declared={declared_size}, actual={self._written[n]}")

        meta_bytes = json.dumps(meta).encode("utf-8")
        tmp_path = self._bundle_dir / _BUNDLE_MANIFEST_TMP
        final_path = self._bundle_dir / _BUNDLE_MANIFEST
        with tmp_path.open("wb") as tmp:
            tmp.write(meta_bytes)
            tmp.flush()
            try:
                os.fsync(tmp.fileno())
            except OSError:
                pass
        os.rename(tmp_path, final_path)
        try:
            dir_fd = os.open(str(self._bundle_dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        self._committed = True

    async def abort(self) -> None:
        if self._committed:
            return
        shutil.rmtree(self._bundle_dir, ignore_errors=True)


def _looks_like_bundle_dir(candidate: Path) -> bool:
    """Return True if the given directory is itself a bundle (committed or in-flight).

    Committed: ``meta.json`` sidecar is present.
    In-flight: the directory contains regular files AND no subdirectories
    (because bundle file names are stored flat under the bundle directory —
    a directory that also has subdirs is a namespace intermediate, not a
    bundle).
    """
    if not candidate.is_dir():
        return False
    if (candidate / _BUNDLE_MANIFEST).is_file():
        return True
    has_files = False
    for child in candidate.iterdir():
        if child.is_dir():
            return False
        if child.is_file():
            has_files = True
    return has_files


def _find_descendant_bundle(root: Path) -> Path | None:
    """Return the path of any descendant bundle directory under ``root``."""
    for entry in root.rglob("*"):
        if entry.is_dir() and _looks_like_bundle_dir(entry):
            return entry
        if entry.is_file() and entry.parent != root:
            # A stray file nested under a non-bundle intermediate dir still
            # signals pre-existing content blocking the new bundle.
            return entry.parent
    return None


def _collision_kind(bundle_dir: Path) -> Literal["shadow_parent", "shadow_child"]:
    """Classify a mkdir(exist_ok=False) failure.

    If the directory already exists and contains content, we treat it as a
    shadow_parent (a bundle lives inside).  Otherwise the failure indicates a
    concurrent reservation — treat it as shadow_child as a safe fallback.
    """
    if bundle_dir.is_dir() and any(bundle_dir.iterdir()):
        return "shadow_parent"
    return "shadow_child"
