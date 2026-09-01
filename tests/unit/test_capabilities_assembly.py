"""Unit tests for ``cassetta.capabilities.build_capabilities_document``.

Covers the invariants from spec.md (FR-001, FR-005, FR-007, FR-008,
FR-011, FR-019), data-model.md §1 and §4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest

import cassetta as _cassetta
from cassetta.capabilities import (
    FEATURES,
    SCHEMA_VERSION,
    SERVER_VERSION,
    SUPPORTED_MODES,
    build_capabilities_document,
)
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import (
    LimitsAdvertisement,
    PolicyContext,
    TTLSettings,
)


@dataclass
class _CapturedEvent:
    event: str
    level: int
    identity_label: str | None
    detail: dict[str, Any] | None


class _EventHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[_CapturedEvent] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.records.append(
            _CapturedEvent(
                event=str(getattr(record, "event", None) or record.getMessage()),
                level=record.levelno,
                identity_label=getattr(record, "identity_label", None),
                detail=getattr(record, "detail", None),
            )
        )


@pytest.fixture
def log_capture() -> Any:
    handler = _EventHandler()
    logger = logging.getLogger("cassetta")
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


class _FakePolicy:
    """Minimal LimitsPolicy stand-in for assembly tests."""

    def __init__(
        self,
        *,
        limits: LimitsAdvertisement,
        ttls: TTLSettings,
    ) -> None:
        self._limits = limits
        self._ttls = ttls

    async def evaluate_upload(self, ctx: Any, manifest: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def evaluate_download(self, ctx: Any, entry: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def advertise_limits(self, ctx: PolicyContext) -> LimitsAdvertisement:
        return self._limits

    def ttls(self, ctx: PolicyContext) -> TTLSettings:
        return self._ttls

    async def advertise_features(self, ctx: PolicyContext) -> list[str]:
        return list(FEATURES)


def _known_limits() -> LimitsAdvertisement:
    return {
        "per_file_max": 1024,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": 25,
        "max_inline_size": 512,
    }


def _known_ttls() -> TTLSettings:
    return {
        "upload_token_ttl": 300,
        "download_claim_ttl": 600,
        "passive_gc_min_age": 3600,
        "passive_gc_interval": 900,
    }


def _ctx(label: str = "agent-a") -> PolicyContext:
    return PolicyContext(identity=Identity(label=label))


def test_module_constants_are_fixed() -> None:
    assert SCHEMA_VERSION == 1
    assert SUPPORTED_MODES == ("inline", "batch", "reference")
    assert FEATURES == ("peek", "batch_upload", "reference_download", "rest_send_init")
    # Must resolve to a non-empty semver-ish string.
    assert isinstance(SERVER_VERSION, str)
    assert SERVER_VERSION
    # In a normal install, matches the package's __version__.
    assert SERVER_VERSION == _cassetta.__version__


@pytest.mark.asyncio
async def test_document_has_six_top_level_keys_in_canonical_order(
    log_capture: _EventHandler,
) -> None:
    policy = _FakePolicy(limits=_known_limits(), ttls=_known_ttls())
    doc = await build_capabilities_document(policy, _ctx(), via="mcp")
    assert list(doc.keys()) == [
        "schema_version",
        "server_version",
        "supported_modes",
        "limits",
        "ttls",
        "features",
    ]


@pytest.mark.asyncio
async def test_schema_version_is_one(log_capture: _EventHandler) -> None:
    policy = _FakePolicy(limits=_known_limits(), ttls=_known_ttls())
    doc = await build_capabilities_document(policy, _ctx(), via="mcp")
    assert doc["schema_version"] == 1


@pytest.mark.asyncio
async def test_server_version_matches_package_version(
    log_capture: _EventHandler,
) -> None:
    policy = _FakePolicy(limits=_known_limits(), ttls=_known_ttls())
    doc = await build_capabilities_document(policy, _ctx(), via="rest")
    assert doc["server_version"] == _cassetta.__version__


@pytest.mark.asyncio
async def test_supported_modes_and_features_are_lists(
    log_capture: _EventHandler,
) -> None:
    policy = _FakePolicy(limits=_known_limits(), ttls=_known_ttls())
    doc = await build_capabilities_document(policy, _ctx(), via="cli")
    assert doc["supported_modes"] == ["inline", "batch", "reference"]
    assert doc["features"] == ["peek", "batch_upload", "reference_download", "rest_send_init"]


@pytest.mark.asyncio
async def test_limits_block_is_verbatim_from_policy(
    log_capture: _EventHandler,
) -> None:
    expected = _known_limits()
    policy = _FakePolicy(limits=expected, ttls=_known_ttls())
    doc = await build_capabilities_document(policy, _ctx(), via="mcp")
    assert doc["limits"] == expected


@pytest.mark.asyncio
async def test_ttls_block_is_verbatim_from_policy(log_capture: _EventHandler) -> None:
    expected = _known_ttls()
    policy = _FakePolicy(limits=_known_limits(), ttls=expected)
    doc = await build_capabilities_document(policy, _ctx(), via="mcp")
    assert doc["ttls"] == expected


@pytest.mark.asyncio
async def test_emits_one_capabilities_queried_debug_log(
    log_capture: _EventHandler,
) -> None:
    policy = _FakePolicy(limits=_known_limits(), ttls=_known_ttls())
    await build_capabilities_document(policy, _ctx(label="agent-a"), via="mcp")
    events = [r for r in log_capture.records if r.event == "capabilities.queried"]
    assert len(events) == 1
    event = events[0]
    assert event.level == logging.DEBUG
    assert event.identity_label == "agent-a"
    assert event.detail == {"via": "mcp"}


@pytest.mark.asyncio
async def test_no_info_or_higher_logs(log_capture: _EventHandler) -> None:
    policy = _FakePolicy(limits=_known_limits(), ttls=_known_ttls())
    await build_capabilities_document(policy, _ctx(), via="rest")
    info_plus = [r for r in log_capture.records if r.level >= logging.INFO]
    assert info_plus == []


@pytest.mark.asyncio
async def test_via_field_propagates_into_log(log_capture: _EventHandler) -> None:
    policy = _FakePolicy(limits=_known_limits(), ttls=_known_ttls())
    for via in ("mcp", "rest", "cli"):
        await build_capabilities_document(policy, _ctx(), via=via)
    vias = [
        r.detail["via"] for r in log_capture.records if r.event == "capabilities.queried" and isinstance(r.detail, dict)
    ]
    assert vias == ["mcp", "rest", "cli"]


@pytest.mark.asyncio
async def test_two_identities_produce_same_document(
    log_capture: _EventHandler,
) -> None:
    policy = _FakePolicy(limits=_known_limits(), ttls=_known_ttls())
    doc_a = await build_capabilities_document(
        policy,
        _ctx(label="agent-a"),
        via="mcp",
    )
    doc_b = await build_capabilities_document(
        policy,
        _ctx(label="agent-b"),
        via="mcp",
    )
    # Document body is identity-independent.
    assert doc_a == doc_b
    # But the emitted logs differ in identity_label.
    labels = [r.identity_label for r in log_capture.records if r.event == "capabilities.queried"]
    assert labels == ["agent-a", "agent-b"]
