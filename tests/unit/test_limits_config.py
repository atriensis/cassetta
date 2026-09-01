"""Tests for LimitsConfig + load_limits_config() (brief 513)."""

from __future__ import annotations

import pytest

from cassetta.config import LimitsConfig, load_limits_config


def test_defaults_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "CASSETTA_PER_FILE_MAX",
        "CASSETTA_PER_BUNDLE_TOTAL_MAX",
        "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
        "CASSETTA_MAX_INLINE_SIZE",
        "CASSETTA_UPLOAD_TOKEN_TTL",
        "CASSETTA_DOWNLOAD_CLAIM_TTL",
        "CASSETTA_PASSIVE_GC_MIN_AGE",
        "CASSETTA_PASSIVE_GC_INTERVAL",
        "CASSETTA_MAX_FILE_SIZE",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = load_limits_config()
    assert cfg == LimitsConfig(
        per_file_max=None,
        per_bundle_total_max=None,
        per_bundle_file_count_max=25,
        max_inline_size=102400,
        upload_token_ttl=300,
        download_claim_ttl=300,
        passive_gc_min_age=3600,
        passive_gc_interval=600,
    )


def test_each_env_var_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CASSETTA_PER_FILE_MAX", "1024")
    monkeypatch.setenv("CASSETTA_PER_BUNDLE_TOTAL_MAX", "2048")
    monkeypatch.setenv("CASSETTA_PER_BUNDLE_FILE_COUNT_MAX", "10")
    monkeypatch.setenv("CASSETTA_MAX_INLINE_SIZE", "4096")
    monkeypatch.setenv("CASSETTA_UPLOAD_TOKEN_TTL", "60")
    monkeypatch.setenv("CASSETTA_DOWNLOAD_CLAIM_TTL", "90")
    monkeypatch.setenv("CASSETTA_PASSIVE_GC_MIN_AGE", "7200")
    monkeypatch.setenv("CASSETTA_PASSIVE_GC_INTERVAL", "1200")
    monkeypatch.delenv("CASSETTA_MAX_FILE_SIZE", raising=False)

    cfg = load_limits_config()
    assert cfg == LimitsConfig(
        per_file_max=1024,
        per_bundle_total_max=2048,
        per_bundle_file_count_max=10,
        max_inline_size=4096,
        upload_token_ttl=60,
        download_claim_ttl=90,
        passive_gc_min_age=7200,
        passive_gc_interval=1200,
    )


@pytest.mark.parametrize(
    "var",
    [
        "CASSETTA_PER_FILE_MAX",
        "CASSETTA_PER_BUNDLE_TOTAL_MAX",
        "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
        "CASSETTA_MAX_INLINE_SIZE",
    ],
)
def test_empty_string_on_cap_fields_unsets(
    monkeypatch: pytest.MonkeyPatch,
    var: str,
) -> None:
    monkeypatch.setenv(var, "")
    cfg = load_limits_config()
    field = var.removeprefix("CASSETTA_").lower()
    assert getattr(cfg, field) is None


@pytest.mark.parametrize(
    "var",
    [
        "CASSETTA_UPLOAD_TOKEN_TTL",
        "CASSETTA_DOWNLOAD_CLAIM_TTL",
        "CASSETTA_PASSIVE_GC_MIN_AGE",
        "CASSETTA_PASSIVE_GC_INTERVAL",
    ],
)
def test_empty_string_on_ttl_fields_returns_default(
    monkeypatch: pytest.MonkeyPatch,
    var: str,
) -> None:
    monkeypatch.setenv(var, "")
    cfg = load_limits_config()
    defaults = {
        "CASSETTA_UPLOAD_TOKEN_TTL": 300,
        "CASSETTA_DOWNLOAD_CLAIM_TTL": 300,
        "CASSETTA_PASSIVE_GC_MIN_AGE": 3600,
        "CASSETTA_PASSIVE_GC_INTERVAL": 600,
    }
    field = var.removeprefix("CASSETTA_").lower()
    assert getattr(cfg, field) == defaults[var]


@pytest.mark.parametrize(
    "var",
    [
        "CASSETTA_PER_FILE_MAX",
        "CASSETTA_PER_BUNDLE_TOTAL_MAX",
        "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
        "CASSETTA_MAX_INLINE_SIZE",
        "CASSETTA_UPLOAD_TOKEN_TTL",
        "CASSETTA_DOWNLOAD_CLAIM_TTL",
        "CASSETTA_PASSIVE_GC_MIN_AGE",
        "CASSETTA_PASSIVE_GC_INTERVAL",
    ],
)
def test_invalid_integer_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    var: str,
) -> None:
    monkeypatch.setenv(var, "not-a-number")
    with pytest.raises(SystemExit):
        load_limits_config()


@pytest.mark.parametrize(
    "var",
    [
        "CASSETTA_PER_FILE_MAX",
        "CASSETTA_PER_BUNDLE_TOTAL_MAX",
        "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
        "CASSETTA_MAX_INLINE_SIZE",
    ],
)
def test_negative_cap_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    var: str,
) -> None:
    monkeypatch.setenv(var, "-1")
    with pytest.raises(SystemExit):
        load_limits_config()


@pytest.mark.parametrize(
    "var",
    [
        "CASSETTA_UPLOAD_TOKEN_TTL",
        "CASSETTA_DOWNLOAD_CLAIM_TTL",
        "CASSETTA_PASSIVE_GC_MIN_AGE",
        "CASSETTA_PASSIVE_GC_INTERVAL",
    ],
)
def test_non_positive_ttl_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    var: str,
) -> None:
    monkeypatch.setenv(var, "0")
    with pytest.raises(SystemExit):
        load_limits_config()


def test_stale_max_file_size_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CASSETTA_MAX_FILE_SIZE", "999999")
    for var in (
        "CASSETTA_PER_FILE_MAX",
        "CASSETTA_PER_BUNDLE_TOTAL_MAX",
        "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
        "CASSETTA_MAX_INLINE_SIZE",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = load_limits_config()
    captured = capsys.readouterr()
    assert "CASSETTA_MAX_FILE_SIZE" in captured.err
    assert cfg.per_file_max is None
