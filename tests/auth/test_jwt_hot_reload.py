"""JWT primary-key hot-reload tests (Brief 531 US3, T027-T033)."""

from __future__ import annotations

import asyncio
import base64
import logging
import signal
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from cassetta.auth import jwt_tokens
from cassetta.auth.jwt_hot_reload import (
    JWTKeySlots,
    _handle_rotation,
    _is_single_worker,
    _kid_prefix,
    _resolve_secondary,
    _validate_overlap_ttl,
    init_jwt_hot_reload,
    init_jwt_key_slots,
)

_TEST_KEY_A = b"a" * 36
_TEST_KEY_B = b"b" * 36
_TEST_KEY_C = b"c" * 36


def _b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


@pytest.fixture
def cassetta_log_capture() -> Iterator[list[logging.LogRecord]]:
    """Capture records emitted on the ``cassetta`` logger."""
    captured: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Handler()
    handler.setLevel(logging.DEBUG)
    target = logging.getLogger("cassetta")
    target.addHandler(handler)
    prior_level = target.level
    target.setLevel(logging.DEBUG)
    try:
        yield captured
    finally:
        target.removeHandler(handler)
        target.setLevel(prior_level)


def _record(captured: list[logging.LogRecord], event: str) -> logging.LogRecord | None:
    for rec in captured:
        if getattr(rec, "event", None) == event:
            return rec
    return None


def _records(captured: list[logging.LogRecord], event: str) -> list[logging.LogRecord]:
    return [r for r in captured if getattr(r, "event", None) == event]


# ---------------------------------------------------------------------------
# T027 — JWTKeySlots + _resolve_secondary helpers
# ---------------------------------------------------------------------------


class TestJWTKeySlots:
    def test_constructable_with_required_fields(self) -> None:
        slots = JWTKeySlots(
            primary=_TEST_KEY_A,
            secondary=None,
            last_rotation_ts=None,
            overlap_ttl_seconds=600,
        )
        assert slots.primary == _TEST_KEY_A
        assert slots.secondary is None

    def test_resolve_secondary_returns_initial_until_rotation(self) -> None:
        slots = JWTKeySlots(
            primary=_TEST_KEY_A,
            secondary=_TEST_KEY_B,
            last_rotation_ts=None,
            overlap_ttl_seconds=600,
        )
        # ``last_rotation_ts is None`` → initial-boot secondary stands.
        assert _resolve_secondary(slots) == _TEST_KEY_B
        assert slots.secondary == _TEST_KEY_B

    def test_resolve_secondary_within_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        slots = JWTKeySlots(
            primary=_TEST_KEY_A,
            secondary=_TEST_KEY_B,
            last_rotation_ts=100.0,
            overlap_ttl_seconds=600,
        )
        monkeypatch.setattr(time, "monotonic", lambda: 200.0)
        assert _resolve_secondary(slots) == _TEST_KEY_B
        assert slots.secondary == _TEST_KEY_B

    def test_resolve_secondary_lazy_drop_after_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        slots = JWTKeySlots(
            primary=_TEST_KEY_A,
            secondary=_TEST_KEY_B,
            last_rotation_ts=100.0,
            overlap_ttl_seconds=600,
        )
        monkeypatch.setattr(time, "monotonic", lambda: 700.5)
        assert _resolve_secondary(slots) is None
        assert slots.secondary is None  # mutated in place

    def test_kid_prefix_is_16_hex_chars(self) -> None:
        kid = _kid_prefix(_TEST_KEY_A)
        assert len(kid) == 16
        assert all(c in "0123456789abcdef" for c in kid)
        # Distinct keys → distinct kids.
        assert _kid_prefix(_TEST_KEY_A) != _kid_prefix(_TEST_KEY_B)


# ---------------------------------------------------------------------------
# T028, T029, T030, T031 — rotation flows
# ---------------------------------------------------------------------------


class _StubConfig:
    """Minimal stand-in for AppConfig with the fields ``_handle_rotation`` reads."""

    def __init__(
        self,
        jwt_primary_key: bytes,
        jwt_secondary_key: bytes | None,
        overlap_ttl: int = 600,
    ) -> None:
        self.jwt_primary_key = jwt_primary_key
        self.jwt_secondary_key = jwt_secondary_key
        self.jwt_key_overlap_ttl = overlap_ttl

        class _Limits:
            download_claim_ttl = 300
            upload_token_ttl = 300

        self.limits = _Limits()


class _StubApp:
    """Stub for :class:`fastapi.FastAPI` used as ``app.state``-only carrier."""

    def __init__(self, config: _StubConfig) -> None:
        self.state = type("S", (), {})()
        self.state.config = config
        self.state.jwt_keys = JWTKeySlots(
            primary=config.jwt_primary_key,
            secondary=config.jwt_secondary_key,
            last_rotation_ts=None,
            overlap_ttl_seconds=config.jwt_key_overlap_ttl,
        )
        self.state.jwt_keys_lock = asyncio.Lock()


@pytest.fixture
def stub_app(monkeypatch: pytest.MonkeyPatch) -> _StubApp:
    monkeypatch.delenv("CASSETTA_JWT_KEY_FILE", raising=False)
    return _StubApp(_StubConfig(_TEST_KEY_A, None, overlap_ttl=600))


class TestRotationHappyPath:
    @pytest.mark.asyncio
    async def test_swap_promotes_old_primary_to_secondary(
        self,
        stub_app: _StubApp,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cassetta_log_capture: list[logging.LogRecord],
    ) -> None:
        key_file = tmp_path / "jwt.key"
        key_file.write_text(_b64(_TEST_KEY_B))
        monkeypatch.setenv("CASSETTA_JWT_KEY_FILE", str(key_file))

        await _handle_rotation(stub_app)  # type: ignore[arg-type]

        assert stub_app.state.jwt_keys.primary == _TEST_KEY_B
        assert stub_app.state.jwt_keys.secondary == _TEST_KEY_A
        assert stub_app.state.jwt_keys.last_rotation_ts is not None

        rec = _record(cassetta_log_capture, "jwt.key_rotated")
        assert rec is not None
        detail = rec.detail  # type: ignore[attr-defined]
        assert detail["previous_kid"] == _kid_prefix(_TEST_KEY_A)
        assert detail["new_kid"] == _kid_prefix(_TEST_KEY_B)
        assert detail["previous_kid"] != detail["new_kid"]
        # ISO timestamps round-trip.
        assert datetime.fromisoformat(detail["rotated_at"])
        assert datetime.fromisoformat(detail["overlap_until"])

    @pytest.mark.asyncio
    async def test_byte_identical_rotation_is_noop(
        self,
        stub_app: _StubApp,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cassetta_log_capture: list[logging.LogRecord],
    ) -> None:
        key_file = tmp_path / "jwt.key"
        key_file.write_text(_b64(_TEST_KEY_A))  # same as current primary
        monkeypatch.setenv("CASSETTA_JWT_KEY_FILE", str(key_file))

        await _handle_rotation(stub_app)  # type: ignore[arg-type]

        assert stub_app.state.jwt_keys.primary == _TEST_KEY_A
        assert stub_app.state.jwt_keys.secondary is None
        assert stub_app.state.jwt_keys.last_rotation_ts is None
        assert _record(cassetta_log_capture, "jwt.key_rotated") is None


class TestRotationFailures:
    @pytest.mark.asyncio
    async def test_missing_file_keeps_slots(
        self,
        stub_app: _StubApp,
        monkeypatch: pytest.MonkeyPatch,
        cassetta_log_capture: list[logging.LogRecord],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv(
            "CASSETTA_JWT_KEY_FILE",
            str(tmp_path / "does-not-exist"),
        )
        await _handle_rotation(stub_app)  # type: ignore[arg-type]
        assert stub_app.state.jwt_keys.primary == _TEST_KEY_A
        rec = _record(cassetta_log_capture, "jwt.key_rotation_failed")
        assert rec is not None
        assert rec.detail["reason"] == "missing_file"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_empty_file_rejected_as_invalid_material(
        self,
        stub_app: _StubApp,
        monkeypatch: pytest.MonkeyPatch,
        cassetta_log_capture: list[logging.LogRecord],
        tmp_path: Path,
    ) -> None:
        empty = tmp_path / "empty"
        empty.write_text("")
        monkeypatch.setenv("CASSETTA_JWT_KEY_FILE", str(empty))
        await _handle_rotation(stub_app)  # type: ignore[arg-type]
        assert stub_app.state.jwt_keys.primary == _TEST_KEY_A
        rec = _record(cassetta_log_capture, "jwt.key_rotation_failed")
        assert rec is not None
        assert rec.detail["reason"] == "invalid_key_material"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_unset_path_signals_missing_file(
        self,
        stub_app: _StubApp,
        monkeypatch: pytest.MonkeyPatch,
        cassetta_log_capture: list[logging.LogRecord],
    ) -> None:
        monkeypatch.delenv("CASSETTA_JWT_KEY_FILE", raising=False)
        await _handle_rotation(stub_app)  # type: ignore[arg-type]
        rec = _record(cassetta_log_capture, "jwt.key_rotation_failed")
        assert rec is not None
        assert rec.detail["reason"] == "missing_file"  # type: ignore[attr-defined]


class TestDisplacement:
    """FR-035 — overlapping rotations displace the secondary."""

    @pytest.mark.asyncio
    async def test_second_rotation_displaces_first_secondary(
        self,
        stub_app: _StubApp,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        slots = stub_app.state.jwt_keys
        # First rotation A → B.
        key_file = tmp_path / "jwt.key"
        key_file.write_text(_b64(_TEST_KEY_B))
        monkeypatch.setenv("CASSETTA_JWT_KEY_FILE", str(key_file))
        await _handle_rotation(stub_app)  # type: ignore[arg-type]
        assert slots.primary == _TEST_KEY_B and slots.secondary == _TEST_KEY_A

        # Second rotation B → C — A drops, B becomes secondary.
        key_file.write_text(_b64(_TEST_KEY_C))
        await _handle_rotation(stub_app)  # type: ignore[arg-type]
        assert slots.primary == _TEST_KEY_C
        assert slots.secondary == _TEST_KEY_B


# ---------------------------------------------------------------------------
# T032 — boot-time validation warning
# ---------------------------------------------------------------------------


class TestOverlapTtlValidation:
    def test_warning_when_overlap_too_short(
        self,
        cassetta_log_capture: list[logging.LogRecord],
    ) -> None:
        config = _StubConfig(_TEST_KEY_A, None, overlap_ttl=200)
        config.limits = type(  # type: ignore[assignment]
            "L",
            (),
            {"download_claim_ttl": 300, "upload_token_ttl": 300},
        )
        _validate_overlap_ttl(config)  # type: ignore[arg-type]
        records = _records(cassetta_log_capture, "config_validation_warning")
        assert len(records) == 1
        detail = records[0].detail  # type: ignore[attr-defined]
        assert detail == {
            "field": "jwt_key_overlap_ttl",
            "value": 200,
            "min_required": 300,
            "reason": "overlap_shorter_than_token_ttl",
        }

    def test_silent_when_overlap_satisfies(
        self,
        cassetta_log_capture: list[logging.LogRecord],
    ) -> None:
        config = _StubConfig(_TEST_KEY_A, None, overlap_ttl=600)
        config.limits = type(  # type: ignore[assignment]
            "L",
            (),
            {"download_claim_ttl": 300, "upload_token_ttl": 300},
        )
        _validate_overlap_ttl(config)  # type: ignore[arg-type]
        assert _records(cassetta_log_capture, "config_validation_warning") == []


# ---------------------------------------------------------------------------
# T033 — multi-worker disabled path
# ---------------------------------------------------------------------------


class TestMultiWorkerDisabled:
    @pytest.mark.parametrize(
        "var",
        ["WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"],
    )
    def test_is_single_worker_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
        var: str,
    ) -> None:
        for v in (
            "WEB_CONCURRENCY",
            "UVICORN_WORKERS",
            "GUNICORN_WORKERS",
        ):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv(var, "2")
        assert _is_single_worker() is False

    def test_is_single_worker_true_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for v in (
            "WEB_CONCURRENCY",
            "UVICORN_WORKERS",
            "GUNICORN_WORKERS",
        ):
            monkeypatch.delenv(v, raising=False)
        assert _is_single_worker() is True

    @pytest.mark.asyncio
    async def test_init_emits_disabled_event_under_multi_worker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cassetta_log_capture: list[logging.LogRecord],
    ) -> None:
        monkeypatch.setenv("WEB_CONCURRENCY", "2")
        config = _StubConfig(_TEST_KEY_A, None, overlap_ttl=600)
        config.limits = type(  # type: ignore[assignment]
            "L",
            (),
            {"download_claim_ttl": 300, "upload_token_ttl": 300},
        )
        app = _StubApp(config)

        loop = asyncio.get_running_loop()
        prior = loop._signal_handlers.get(signal.SIGHUP)  # type: ignore[attr-defined]

        init_jwt_hot_reload(app)  # type: ignore[arg-type]

        rec = _record(cassetta_log_capture, "jwt.hot_reload_disabled")
        assert rec is not None
        assert rec.detail["reason"] == "multi_worker"  # type: ignore[attr-defined]
        # Sanity: handler not added.
        post = loop._signal_handlers.get(signal.SIGHUP)  # type: ignore[attr-defined]
        assert post == prior


# ---------------------------------------------------------------------------
# Verify-path integration — issue/verify spans a rotation, lazy drop fires
# ---------------------------------------------------------------------------


class TestVerifyPathIntegration:
    """SC-004 — token issued under the demoted primary still verifies
    while inside the overlap window, and is rejected after the window."""

    def _issue(self, *, key: bytes, ttl: int = 60) -> str:
        now = int(time.time())
        return jwt_tokens.sign(
            {
                "exp": now + ttl,
                "nbf": now - 1,
                "iat": now,
                "subject": "test",
            },
            key=key,
        )

    def test_within_overlap_secondary_still_verifies(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        slots = JWTKeySlots(
            primary=_TEST_KEY_B,
            secondary=_TEST_KEY_A,  # demoted at last_rotation_ts.
            last_rotation_ts=100.0,
            overlap_ttl_seconds=600,
        )
        monkeypatch.setattr(time, "monotonic", lambda: 200.0)
        token = self._issue(key=_TEST_KEY_A)

        secondary = _resolve_secondary(slots)
        claims = jwt_tokens.verify(
            token,
            primary=slots.primary,
            secondary=secondary,
        )
        assert claims["subject"] == "test"

    def test_past_overlap_old_token_rejected_lazy_drop(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        slots = JWTKeySlots(
            primary=_TEST_KEY_B,
            secondary=_TEST_KEY_A,
            last_rotation_ts=100.0,
            overlap_ttl_seconds=600,
        )
        token = self._issue(key=_TEST_KEY_A)

        # Past the overlap window — _resolve_secondary returns None
        # AND mutates the slot to drop the secondary in place.
        monkeypatch.setattr(time, "monotonic", lambda: 800.0)
        secondary = _resolve_secondary(slots)
        assert secondary is None
        assert slots.secondary is None

        with pytest.raises(jwt_tokens.TokenInvalidSignature):
            jwt_tokens.verify(
                token,
                primary=slots.primary,
                secondary=secondary,
            )


# ---------------------------------------------------------------------------
# init_jwt_key_slots populates app.state for tests bypassing lifespan
# ---------------------------------------------------------------------------


class TestInitJWTKeySlots:
    def test_populates_app_state_jwt_keys(self) -> None:
        config = _StubConfig(_TEST_KEY_A, _TEST_KEY_B, overlap_ttl=600)
        app = _StubApp(config)
        # Reset to simulate the init pathway (state.jwt_keys un-set).
        app.state = type("S", (), {})()
        app.state.config = config

        init_jwt_key_slots(app)  # type: ignore[arg-type]
        assert isinstance(app.state.jwt_keys, JWTKeySlots)
        assert app.state.jwt_keys.primary == _TEST_KEY_A
        assert app.state.jwt_keys.secondary == _TEST_KEY_B
        assert app.state.jwt_keys.overlap_ttl_seconds == 600
