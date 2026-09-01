"""Wiring tests for LimitsPolicy DI through create_app / lifespan / MCP (brief 513)."""

from __future__ import annotations

import tempfile

import pytest

import cassetta.mcp_server as mcp_server_module
from cassetta.app import create_app
from cassetta.config import LimitsConfig, load_config
from cassetta.defaults.default_limits import CoreLimitsPolicy
from cassetta.dependencies import get_limits_policy
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import (
    DownloadDecision,
    DownloadEntry,
    LimitsAdvertisement,
    PolicyContext,
    TTLSettings,
    UploadDecision,
    UploadManifest,
)


class _FakePolicy:
    async def evaluate_upload(
        self, ctx: PolicyContext, manifest: UploadManifest,
    ) -> UploadDecision:
        return {"mode": "inline", "reason": None}

    async def evaluate_download(
        self, ctx: PolicyContext, entry: DownloadEntry,
    ) -> DownloadDecision:
        return {"mode": "inline", "reason": None}

    def advertise_limits(self, ctx: PolicyContext) -> LimitsAdvertisement:
        return {
            "per_file_max": 1,
            "per_bundle_total_max": 2,
            "per_bundle_file_count_max": 3,
            "max_inline_size": 4,
        }

    def ttls(self, ctx: PolicyContext) -> TTLSettings:
        return {
            "upload_token_ttl": 10,
            "download_claim_ttl": 20,
            "passive_gc_min_age": 30,
            "passive_gc_interval": 40,
        }


@pytest.fixture
def _dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CASSETTA_SETUP_TOKEN", "")
    monkeypatch.setenv("CASSETTA_STORAGE_PATH", tempfile.mkdtemp())
    monkeypatch.setenv("CASSETTA_DEFAULT_TTL", "0")
    monkeypatch.setenv(
        "CASSETTA_JWT_KEY",
        "dGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0LXRlc3QtdGVzdC10ZXN0",  # 36 bytes
    )
    monkeypatch.setenv("CASSETTA_PUBLIC_BASE_URL", "http://localhost:16001")
    monkeypatch.delenv("CASSETTA_KEYS_FILE", raising=False)
    for var in (
        "CASSETTA_PER_FILE_MAX",
        "CASSETTA_PER_BUNDLE_TOTAL_MAX",
        "CASSETTA_PER_BUNDLE_FILE_COUNT_MAX",
        "CASSETTA_MAX_INLINE_SIZE",
        "CASSETTA_UPLOAD_TOKEN_TTL",
        "CASSETTA_DOWNLOAD_CLAIM_TTL",
        "CASSETTA_PASSIVE_GC_MIN_AGE",
        "CASSETTA_PASSIVE_GC_INTERVAL",
        "CASSETTA_JWT_KEY_FILE",
        "CASSETTA_JWT_KEY_SECONDARY",
        "CASSETTA_JWT_KEY_SECONDARY_FILE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_default_limits_policy_from_config(_dev_env: None) -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        policy = app.state.backends.limits_policy
        assert isinstance(policy, CoreLimitsPolicy)
        assert policy._config == app.state.config.limits
        assert mcp_server_module._limits_policy is policy


@pytest.mark.asyncio
async def test_limits_policy_override(_dev_env: None, make_backends) -> None:
    fake = _FakePolicy()
    config = load_config()
    app = create_app(config, backends=make_backends(config, limits_policy=fake))
    async with app.router.lifespan_context(app):
        assert app.state.backends.limits_policy is fake
        assert mcp_server_module._get_limits_policy() is fake


@pytest.mark.asyncio
async def test_get_limits_policy_dependency(
    _dev_env: None, make_backends,
) -> None:
    fake = _FakePolicy()
    config = load_config()
    app = create_app(config, backends=make_backends(config, limits_policy=fake))
    async with app.router.lifespan_context(app):
        got = get_limits_policy(app.state.backends)
        assert got is fake


@pytest.mark.asyncio
async def test_limits_policy_constructed_from_env(
    monkeypatch: pytest.MonkeyPatch, _dev_env: None,
) -> None:
    monkeypatch.setenv("CASSETTA_PER_FILE_MAX", "1234")
    app = create_app()
    async with app.router.lifespan_context(app):
        policy = app.state.backends.limits_policy
        assert isinstance(policy, CoreLimitsPolicy)
        assert app.state.config.limits.per_file_max == 1234
        assert policy._config == app.state.config.limits


def test_identity_for_policy_context_is_importable() -> None:
    ctx = PolicyContext(identity=Identity(label="x"))
    assert ctx.identity.label == "x"


def test_core_limits_policy_construct_with_defaults() -> None:
    assert CoreLimitsPolicy(LimitsConfig()) is not None


def test_mcp_server_module_importable() -> None:
    assert hasattr(mcp_server_module, "configure")
