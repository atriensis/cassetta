"""Unit tests for the centralised auth-observability emission helper.

Covers event/counter pairing, the identity_hint contract, and the
best-effort wrap.
"""

from __future__ import annotations

import logging
from typing import get_args

import pytest

from cassetta.auth.observability import (
    Reason,
    Source,
    emit_auth_failure,
)
from cassetta.protocols.metrics import MetricsProvider

from .conftest import RecordingMetricsProvider

SOURCES = ("rest", "mcp", "download")
REASONS = (
    "missing_bearer",
    "invalid_key",
    "invalid_setup_token",
    "jwt_invalid",
    "jwt_expired",
    "jwt_aud_mismatch",
)


class TestSourceAndReasonLiterals:
    def test_source_literal_exposes_three_values(self) -> None:
        assert set(get_args(Source)) == set(SOURCES)

    def test_reason_literal_exposes_six_values(self) -> None:
        assert set(get_args(Reason)) == set(REASONS)


class TestRecordingFixture:
    def test_recording_provider_satisfies_protocol(
        self,
        recording_metrics: RecordingMetricsProvider,
    ) -> None:
        assert isinstance(recording_metrics, MetricsProvider)


class TestEmitAuthFailure:
    """One log record + one counter increment per call."""

    @pytest.mark.parametrize("source", SOURCES)
    @pytest.mark.parametrize("reason", REASONS)
    def test_emits_event_and_counter_pair(
        self,
        source: str,
        reason: str,
        recording_metrics: RecordingMetricsProvider,
        auth_log_capture: list[logging.LogRecord],
    ) -> None:
        emit_auth_failure(
            metrics=recording_metrics,
            source=source,  # type: ignore[arg-type]
            reason=reason,  # type: ignore[arg-type]
        )

        records = [r for r in auth_log_capture if getattr(r, "event", None) == "auth.failure"]
        assert len(records) == 1
        rec = records[0]
        assert rec.levelno == logging.WARNING
        assert rec.detail == {  # type: ignore[attr-defined]
            "source": source,
            "reason": reason,
            "identity_hint": None,
        }

        increments = recording_metrics.find(
            "cassetta.auth.failures",
            method="increment",
        )
        assert len(increments) == 1
        assert increments[0].tags == {"source": source, "reason": reason}
        assert increments[0].value == 1

    def test_identity_hint_none_passthrough(
        self,
        recording_metrics: RecordingMetricsProvider,
        auth_log_capture: list[logging.LogRecord],
    ) -> None:
        emit_auth_failure(
            metrics=recording_metrics,
            source="rest",
            reason="missing_bearer",
            identity_hint=None,
        )
        rec = next(r for r in auth_log_capture if getattr(r, "event", None) == "auth.failure")
        assert rec.detail["identity_hint"] is None  # type: ignore[attr-defined]

    def test_identity_hint_max_length(
        self,
        recording_metrics: RecordingMetricsProvider,
        auth_log_capture: list[logging.LogRecord],
    ) -> None:
        emit_auth_failure(
            metrics=recording_metrics,
            source="rest",
            reason="invalid_key",
            identity_hint="abcd1234efgh",
        )
        rec = next(r for r in auth_log_capture if getattr(r, "event", None) == "auth.failure")
        assert rec.detail["identity_hint"] == "abcd1234efgh"  # type: ignore[attr-defined]

    def test_identity_hint_short_prefix_passthrough(
        self,
        recording_metrics: RecordingMetricsProvider,
        auth_log_capture: list[logging.LogRecord],
    ) -> None:
        emit_auth_failure(
            metrics=recording_metrics,
            source="rest",
            reason="invalid_key",
            identity_hint="abc",
        )
        rec = next(r for r in auth_log_capture if getattr(r, "event", None) == "auth.failure")
        assert rec.detail["identity_hint"] == "abc"  # type: ignore[attr-defined]

    @pytest.mark.skipif(
        not __debug__,
        reason="defence-in-depth assert is removed under -O",
    )
    def test_identity_hint_too_long_raises_assert(
        self,
        recording_metrics: RecordingMetricsProvider,
    ) -> None:
        with pytest.raises(AssertionError):
            emit_auth_failure(
                metrics=recording_metrics,
                source="rest",
                reason="invalid_key",
                identity_hint="X" * 13,
            )


class TestBestEffortWrap:
    """Emission failures must NOT propagate to the auth path."""

    def test_metrics_increment_raising_does_not_propagate(
        self,
        auth_log_capture: list[logging.LogRecord],
    ) -> None:
        class BoomProvider:
            kind = "boom"

            def increment(
                self,
                name: str,
                value: int = 1,
                tags: dict[str, str] | None = None,
            ) -> None:
                raise RuntimeError("metrics down")

            def observe(
                self,
                name: str,
                value: float,
                tags: dict[str, str] | None = None,
            ) -> None: ...

            def gauge(
                self,
                name: str,
                value: float,
                tags: dict[str, str] | None = None,
            ) -> None: ...

        emit_auth_failure(
            metrics=BoomProvider(),
            source="rest",
            reason="missing_bearer",
        )

        fallbacks = [r for r in auth_log_capture if "auth observability emission failed" in r.getMessage()]
        assert len(fallbacks) == 1
        assert fallbacks[0].levelno == logging.ERROR

    def test_struct_log_raising_does_not_propagate(
        self,
        recording_metrics: RecordingMetricsProvider,
        monkeypatch: pytest.MonkeyPatch,
        auth_log_capture: list[logging.LogRecord],
    ) -> None:
        from cassetta.auth import observability as obs_mod

        def boom_struct_log(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("logging down")

        monkeypatch.setattr(obs_mod, "struct_log", boom_struct_log)

        emit_auth_failure(
            metrics=recording_metrics,
            source="rest",
            reason="missing_bearer",
        )

        fallbacks = [r for r in auth_log_capture if "auth observability emission failed" in r.getMessage()]
        assert len(fallbacks) == 1

    def test_logger_exception_raising_swallowed_silently(
        self,
        recording_metrics: RecordingMetricsProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cassetta.auth import observability as obs_mod

        def boom_struct_log(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("logging down")

        class BoomLogger:
            def exception(self, *_a: object, **_kw: object) -> None:
                raise RuntimeError("catastrophic")

            def warning(self, *_a: object, **_kw: object) -> None: ...

            def log(self, *_a: object, **_kw: object) -> None: ...

        monkeypatch.setattr(obs_mod, "struct_log", boom_struct_log)
        monkeypatch.setattr(obs_mod, "_logger", BoomLogger())

        # MUST NOT raise — auth path's 401/403 must reach the client.
        emit_auth_failure(
            metrics=recording_metrics,
            source="rest",
            reason="missing_bearer",
        )
