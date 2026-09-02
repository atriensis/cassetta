import asyncio
import io
import logging
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from cassetta.app import create_app
from cassetta.config import AppConfig
from cassetta.defaults.factory import BackendConfig, build_core_defaults
from cassetta.mcp_server import configure as configure_mcp
from cassetta.mime import pick_mime
from cassetta.protocols.claim_storage import ClaimStorage
from cassetta.protocols.storage import StorageBackend


@pytest.fixture
def make_backends() -> Callable[..., BackendConfig]:
    """Fixture returning a helper: filesystem defaults with fields swapped.

    Tests that need a full bundle call ``make_backends(config)``; tests
    that need to stub a single Protocol (e.g. an in-memory
    ``ClaimStorage``) call ``make_backends(config, claim_store=stub)``.
    Exposed as a fixture (not a module-level function) so that tests in
    subdirs like ``tests/unit/`` — which pytest's
    ``--import-mode=importlib`` does not place on sys.path alongside the
    parent ``conftest.py`` — still receive it via dependency injection.
    """

    def _make(config: AppConfig, **overrides: Any) -> BackendConfig:
        return replace(build_core_defaults(config), **overrides)

    return _make


async def seed_inbox_bundle(
    backend: StorageBackend,
    recipient: str,
    path: str,
    *,
    content: bytes | None = None,
    files: list[tuple[str, bytes]] | None = None,
    sender: str | None = None,
) -> str:
    """Populate an inbox bundle directly via the backend.

    Replaces the removed legacy PUT/MCP send path for test setup.
    Returns the bundle_id.
    """
    if (content is None) == (files is None):
        raise ValueError("pass exactly one of content or files")
    file_parts: list[tuple[str, bytes]] = list(files) if files is not None else [(path.split("/")[-1], content or b"")]
    bundle_path = f"inbox/{recipient}/{path}"
    bundle_id = uuid.uuid4().hex
    writer = await backend.open_bundle_write(bundle_path)
    try:
        records: list[dict[str, Any]] = []
        for name, data in file_parts:
            await writer.write_file(name, io.BytesIO(data))
            records.append(
                {
                    "name": name,
                    "size": len(data),
                    "mime": pick_mime(name, explicit=None),
                }
            )
        meta: dict[str, Any] = {
            "schema_version": 1,
            "bundle_id": bundle_id,
            "sender": sender,
            "created_at": datetime.now(UTC).isoformat(),
            "content_type": "application/octet-stream",
            "file_count": len(records),
            "files": records,
        }
        await writer.commit(meta)
        return bundle_id
    except Exception:
        await writer.abort()
        raise


async def _start_mcp_manager(app):  # noqa: ANN001
    """Start MCP session manager in a background task, return stop callback."""
    started = asyncio.Event()
    stop = asyncio.Event()

    async def _run() -> None:
        async with app.state.mcp_server.session_manager.run():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_run())
    await started.wait()

    async def _stop() -> None:
        stop.set()
        await task

    return _stop


@pytest.fixture
def storage_dir() -> str:
    """Temporary storage directory for tests."""
    return tempfile.mkdtemp()


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> Iterator[None]:
    """Brief 531 — clear in-memory limiter state between tests.

    The limiter is module-global (shared across the process) so without
    a reset, hits from earlier tests can trip rate-limit rejections in
    later ones. ``MemoryStorage.reset()`` wipes every per-IP bucket.
    """
    from cassetta.rate_limit.limiter import limiter

    limiter._storage.reset()
    try:
        yield
    finally:
        limiter._storage.reset()


# The single seam cloud's conftest overrides to add an Azurite-backed
# axis. Keep the parametrize shape compatible: the default param list
# is ``["filesystem"]`` so core runs the filesystem axis only; cloud
# extends it to ``["filesystem", pytest.param("azurite", marks=...)]``.
ClaimStorageFactory = Callable[..., Awaitable[ClaimStorage]]


@pytest.fixture(params=["filesystem"])
async def claim_storage_factory(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[ClaimStorageFactory]:
    """Yield a ``(base_dir=None) -> ClaimStorage`` async factory.

    See ``specs/517-claimstore-cloud-parity/research.md §Decision 6``.
    """
    from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage

    async def _factory(base_dir: Path | None = None) -> ClaimStorage:
        return FilesystemClaimStorage(base_dir or tmp_path / ".claims")

    yield _factory


_TEST_JWT_KEY_B64 = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"  # 36 bytes


@pytest.fixture(scope="session", autouse=True)
def _core_env_defaults() -> None:
    """Brief 539 — session-wide baseline for the boot-required signing key.

    Since Brief 527 ``load_config()`` requires ``CASSETTA_JWT_KEY`` and
    ``CASSETTA_PUBLIC_BASE_URL``. Before this fixture, core tests that boot the
    app without going through an env-setting fixture free-rode on a key *leaked*
    by another test — so they failed when run in isolation (Constitution
    Principle IX). Providing the key once as a session-wide default makes every
    core test self-sufficient regardless of ordering.

    Idempotent ``setdefault`` keeps it order-invariant (research §5), mirroring
    cloud's ``_cassetta_v514_env_defaults``. Tests that exercise the
    missing-key boot path override via ``monkeypatch.delenv`` (e.g.
    ``unit/test_boot_fails_without_keys.py``), which auto-restores at teardown.
    """
    os.environ.setdefault("CASSETTA_JWT_KEY", _TEST_JWT_KEY_B64)
    os.environ.setdefault("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    os.environ.pop("CASSETTA_JWT_KEY_FILE", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY", None)
    os.environ.pop("CASSETTA_JWT_KEY_SECONDARY_FILE", None)


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    """Brief 539 — restore ``os.environ`` after every test (Principle IX backstop).

    The flagged leakers are migrated to ``monkeypatch`` at the call site, but a
    long tail of ~40 core test files still set ``CASSETTA_*`` via raw
    ``os.environ`` without cleanup (e.g. limit/broadcast caps). Rather than
    rewrite them all, this autouse guard snapshots the environment at function
    setup and restores it at teardown, so no raw write can leak into a later
    test under randomized ordering. This is the safety net described in
    ``research.md §10``.

    Order-invariance: the snapshot is taken *after* the session-scoped
    ``_core_env_defaults`` has applied its baseline (higher scope sets up
    first), so the restore target is identical for every test regardless of
    order. Composes with ``monkeypatch``: as an autouse function fixture it sets
    up before a test's directly-requested ``monkeypatch``, hence tears down
    *after* monkeypatch's own undo — the two never fight.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        for key in set(os.environ) - set(snapshot):
            del os.environ[key]
        for key, value in snapshot.items():
            if os.environ.get(key) != value:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """Brief 539 — restore logger state after every test (Principle IX, research §3).

    ``configure_logging()`` mutates process-global logger state (handlers,
    level, ``propagate``), and ``logging.getLogger`` interns every name for the
    life of the interpreter — so a tree one test configures is still configured
    when the next one runs. That has bitten this suite before: a test that
    disabled propagation broke later caplog-based assertions when the suite ran
    whole, and ``test_gc_reaper``/``test_limits_policy_evaluate`` free-rode on a
    leaked low ``cassetta`` level. This autouse guard snapshots and restores
    ``handlers``/``level``/``propagate`` around each test for the project's own
    two trees and for the example tree the ``extra_log_trees`` tests supply, so
    a logging tweak in one test cannot leak into the next. Placed at the core
    root (not the four subtree conftests named in the brief) for DRY and so it
    also covers ``unit/``.
    """
    names = ("cassetta", "cassetta.auth", "example_embedder")
    saved = [(lg, list(lg.handlers), lg.level, lg.propagate) for lg in (logging.getLogger(n) for n in names)]
    try:
        yield
    finally:
        for lg, handlers, level, propagate in saved:
            lg.handlers[:] = handlers
            lg.setLevel(level)
            lg.propagate = propagate


@pytest.fixture
def env_setup(storage_dir: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set environment variables for dev mode test runs.

    Brief 539: uses ``monkeypatch`` so every var is auto-restored at function
    teardown — no ``CASSETTA_*`` value leaks into a later test (Principle IX).
    """
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", storage_dir)
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.setenv("CASSETTA_MAX_FILE_SIZE", "1048576")
    monkeypatch.setenv(
        "CASSETTA_MCP_ALLOWED_HOSTS",
        "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001",
    )
    monkeypatch.setenv("CASSETTA_JWT_KEY", _TEST_JWT_KEY_B64)
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    monkeypatch.delenv("CASSETTA_JWT_KEY_FILE", raising=False)
    monkeypatch.delenv("CASSETTA_JWT_KEY_SECONDARY", raising=False)
    monkeypatch.delenv("CASSETTA_JWT_KEY_SECONDARY_FILE", raising=False)
    monkeypatch.delenv("CASSETTA_KEYS_FILE", raising=False)


@pytest.fixture
async def client(
    env_setup: None,
    storage_dir: str,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async test client with dev mode (no auth required)."""
    app = create_app()
    config = app.state.config
    backends = app.state.backends
    # Configure MCP manually: ASGITransport below runs without lifespan,
    # so ``app.py``'s lifespan-level ``configure_mcp`` call never fires.
    configure_mcp(config, backends)

    stop_mcp = await _start_mcp_manager(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as c:
        yield c

    await stop_mcp()


@pytest.fixture
def auth_log_capture() -> Iterator[list[logging.LogRecord]]:
    """Capture log records emitted on the ``cassetta.auth`` logger.

    Workaround for Brief 529 tests: pytest's ``caplog`` attaches to the
    root logger, so once ``configure_logging`` sets ``cassetta.propagate
    = False`` no records reach the LogCaptureHandler. This fixture binds
    a tiny in-memory handler directly to ``cassetta.auth``.
    """
    captured: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Handler()
    handler.setLevel(logging.DEBUG)
    target = logging.getLogger("cassetta.auth")
    target.addHandler(handler)
    prior_level = target.level
    target.setLevel(logging.DEBUG)
    try:
        yield captured
    finally:
        target.removeHandler(handler)
        target.setLevel(prior_level)


@pytest.fixture
async def auth_client(
    storage_dir: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Async test client with auth enabled. Returns (client, setup_token).

    Brief 539: ``monkeypatch`` auto-restores every var at teardown (Principle IX).
    """
    setup_token = "test-setup-token-123"
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", setup_token)
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", storage_dir)
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.setenv("CASSETTA_MAX_FILE_SIZE", "1048576")
    monkeypatch.setenv(
        "CASSETTA_MCP_ALLOWED_HOSTS",
        "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001",
    )
    monkeypatch.setenv("CASSETTA_JWT_KEY", _TEST_JWT_KEY_B64)
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    monkeypatch.delenv("CASSETTA_JWT_KEY_FILE", raising=False)
    monkeypatch.delenv("CASSETTA_JWT_KEY_SECONDARY", raising=False)
    monkeypatch.delenv("CASSETTA_JWT_KEY_SECONDARY_FILE", raising=False)
    monkeypatch.delenv("CASSETTA_KEYS_FILE", raising=False)

    app = create_app()
    config = app.state.config
    backends = app.state.backends
    configure_mcp(config, backends)

    stop_mcp = await _start_mcp_manager(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:16001",
    ) as c:
        yield c, setup_token

    await stop_mcp()
