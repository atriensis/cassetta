"""JWT primary-key hot-reload (Brief 531 US3).

Lives at ``app.state.jwt_keys`` as a mutable :class:`JWTKeySlots`
holder. Initialised from boot-time ``config.jwt_primary_key`` /
``config.jwt_secondary_key`` and mutated in place by SIGHUP-driven
rotations. The verify path reads ``slots.primary`` and the
:func:`_resolve_secondary` helper at call time — never the boot-time
``AppConfig`` field once rotation has occurred.

Multi-worker safety: ``loop.add_signal_handler`` only registers the
handler when ``_is_single_worker()`` returns True; otherwise emits
``jwt.hot_reload_disabled`` once at startup and bows out.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from cassetta.structured_log import struct_log

if TYPE_CHECKING:
    from fastapi import FastAPI

    from cassetta.config import AppConfig

_logger = logging.getLogger("cassetta")

_MIN_HS256_KEY_BYTES = 32


@dataclass(slots=True)
class JWTKeySlots:
    """Mutable runtime holder for the primary + verify-only secondary."""

    primary: bytes
    secondary: bytes | None
    last_rotation_ts: float | None
    overlap_ttl_seconds: int


def _kid_prefix(key_bytes: bytes) -> str:
    """First 16 hex chars of the key's SHA-256 — never the key itself."""
    return hashlib.sha256(key_bytes).hexdigest()[:16]


def _resolve_secondary(slots: JWTKeySlots) -> bytes | None:
    """Return the active secondary, lazily dropping it past the overlap TTL.

    FR-037b — "lazy drop". FR-037c — read both fields once at entry so a
    concurrent SIGHUP cannot make the decision torn-read inconsistent.
    """
    last = slots.last_rotation_ts
    ttl = slots.overlap_ttl_seconds
    if last is None:
        # Initial-boot secondary (from CASSETTA_JWT_KEY_SECONDARY*) —
        # never expires until a rotation happens.
        return slots.secondary
    if time.monotonic() - last >= ttl:
        slots.secondary = None
        return None
    return slots.secondary


def _is_single_worker() -> bool:
    """True when no env var indicates multi-worker, else False (R8)."""
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        value = os.environ.get(var)
        if value is None:
            continue
        try:
            return int(value) == 1
        except ValueError:
            continue
    return True


def _validate_overlap_ttl(config: AppConfig) -> None:
    """Emit ``config_validation_warning`` when the overlap is too short."""
    overlap = config.jwt_key_overlap_ttl
    min_required = max(
        config.limits.download_claim_ttl,
        config.limits.upload_token_ttl,
    )
    if overlap < min_required:
        struct_log(
            _logger, logging.WARNING, "config_validation_warning",
            detail={
                "field": "jwt_key_overlap_ttl",
                "value": overlap,
                "min_required": min_required,
                "reason": "overlap_shorter_than_token_ttl",
            },
        )


def _decode_key_material(raw: bytes) -> bytes | None:
    """Decode the file's stripped base64-encoded key bytes.

    Returns the decoded key on success, or None when the input is empty
    or not a valid base64 ≥32-byte HS256 key.
    """
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        decoded = base64.b64decode(stripped, validate=True)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None
    if len(decoded) < _MIN_HS256_KEY_BYTES:
        return None
    return decoded


async def _handle_rotation(app: FastAPI) -> None:
    """SIGHUP body: re-read the key file, swap slots, emit the event."""
    config: AppConfig = app.state.config
    file_path = os.environ.get("CASSETTA_JWT_KEY_FILE", "").strip()

    if not file_path:
        struct_log(
            _logger, logging.ERROR, "jwt.key_rotation_failed",
            detail={"reason": "missing_file", "path": file_path},
        )
        return

    try:
        with open(file_path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        struct_log(
            _logger, logging.ERROR, "jwt.key_rotation_failed",
            detail={"reason": "missing_file", "path": file_path},
        )
        return
    except PermissionError:
        struct_log(
            _logger, logging.ERROR, "jwt.key_rotation_failed",
            detail={"reason": "permission_denied", "path": file_path},
        )
        return
    except OSError:
        struct_log(
            _logger, logging.ERROR, "jwt.key_rotation_failed",
            detail={"reason": "read_error", "path": file_path},
        )
        return

    new_key = _decode_key_material(raw)
    if new_key is None:
        struct_log(
            _logger, logging.ERROR, "jwt.key_rotation_failed",
            detail={"reason": "invalid_key_material", "path": file_path},
        )
        return

    slots: JWTKeySlots = app.state.jwt_keys
    lock: asyncio.Lock = app.state.jwt_keys_lock
    async with lock:
        if new_key == slots.primary:
            return  # FR-034: byte-identical rotation is a no-op.
        previous_primary = slots.primary
        slots.primary = new_key
        slots.secondary = previous_primary
        slots.last_rotation_ts = time.monotonic()
        slots.overlap_ttl_seconds = config.jwt_key_overlap_ttl

    rotated_at = datetime.now(UTC)
    overlap_until = rotated_at + timedelta(
        seconds=slots.overlap_ttl_seconds,
    )
    struct_log(
        _logger, logging.INFO, "jwt.key_rotated",
        detail={
            "rotated_at": rotated_at.isoformat(),
            "previous_kid": _kid_prefix(previous_primary),
            "new_kid": _kid_prefix(new_key),
            "overlap_until": overlap_until.isoformat(),
        },
    )


def _install_sighup_handler(app: FastAPI) -> None:
    """Register the SIGHUP handler against the running asyncio loop."""
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(
        signal.SIGHUP,
        lambda: asyncio.create_task(_handle_rotation(app)),
    )


def init_jwt_key_slots(app: FastAPI) -> None:
    """Populate ``app.state.jwt_keys`` from boot-time ``AppConfig``.

    Called from ``create_app`` (NOT lifespan) so even ASGI test clients
    that bypass lifespan see a populated holder. The lock is a stub
    (no event loop yet); ``init_jwt_hot_reload`` re-creates it inside
    the running loop so the SIGHUP handler can grab it without
    cross-loop reuse.
    """
    config: AppConfig = app.state.config
    app.state.jwt_keys = JWTKeySlots(
        primary=config.jwt_primary_key,
        secondary=config.jwt_secondary_key,
        last_rotation_ts=None,
        overlap_ttl_seconds=config.jwt_key_overlap_ttl,
    )


def init_jwt_hot_reload(app: FastAPI) -> None:
    """Install the SIGHUP handler (single-worker only) and emit warnings.

    Called from ``lifespan()`` after ``config_loaded``. Slots themselves
    are populated earlier by ``init_jwt_key_slots``; this function only
    handles the runtime concerns that need a running asyncio loop.
    Idempotent — a second call simply re-installs the handler.
    """
    config: AppConfig = app.state.config
    _validate_overlap_ttl(config)
    app.state.jwt_keys_lock = asyncio.Lock()

    if not _is_single_worker():
        struct_log(
            _logger, logging.INFO, "jwt.hot_reload_disabled",
            detail={"reason": "multi_worker"},
        )
        return

    try:
        _install_sighup_handler(app)
    except (NotImplementedError, RuntimeError):
        # Windows-native loop or non-asyncio test contexts. Same
        # operator-facing signal as the multi-worker case so dashboards
        # surface the absence of hot-reload.
        struct_log(
            _logger, logging.INFO, "jwt.hot_reload_disabled",
            detail={"reason": "signal_handler_unavailable"},
        )
