"""FR-020 — core's ``create_app`` emits ``claim_storage_backend=filesystem``
at INFO during lifespan startup on the default path."""

from __future__ import annotations

import logging
import os
import tempfile

import pytest

from cassetta.app import create_app


@pytest.mark.asyncio
async def test_startup_log_filesystem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage_dir = tempfile.mkdtemp()
    os.environ["CASSETTA_SETUP_TOKEN"] = ""
    os.environ["CASSETTA_STORAGE_PATH"] = storage_dir
    os.environ["CASSETTA_DEFAULT_TTL"] = "0"
    os.environ["CASSETTA_MCP_ALLOWED_HOSTS"] = "localhost,localhost:16001,127.0.0.1,127.0.0.1:16001"
    os.environ["CASSETTA_JWT_KEY"] = "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0"
    os.environ["CASSETTA_PUBLIC_BASE_URL"] = "http://localhost:16001"
    os.environ.pop("CASSETTA_KEYS_FILE", None)

    caplog.set_level(logging.INFO, logger="cassetta")
    app = create_app()
    # cassetta logger is propagate=False AND configure_logging()
    # (called inside create_app) clears all handlers. Attach the
    # caplog handler AFTER create_app so our log lines land in
    # caplog.records during lifespan.
    cassetta_logger = logging.getLogger("cassetta")
    cassetta_logger.addHandler(caplog.handler)
    try:
        # Drive the FastAPI lifespan manually — httpx's ASGITransport
        # does not run it automatically, and the startup-log emission
        # is inside the lifespan context.
        async with app.router.lifespan_context(app):
            pass
    finally:
        cassetta_logger.removeHandler(caplog.handler)

    # Brief 533 FR-026: emission converted to struct_log. The record now
    # carries ``event="claim_storage_backend"`` with ``detail.kind`` set
    # to the active backend (``filesystem`` on the default path).
    matching = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "claim_storage_backend"
        and (getattr(r, "detail", None) or {}).get("kind") == "filesystem"
    ]
    assert matching, (
        f"expected claim_storage_backend struct_log with kind=filesystem; "
        f"got {[(r.getMessage(), getattr(r, 'detail', None)) for r in caplog.records]!r}"
    )
