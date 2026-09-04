"""Tests for CoreLimitsPolicy.advertise_limits / .ttls."""

from __future__ import annotations

from cassetta.config import LimitsConfig
from cassetta.defaults.default_limits import CoreLimitsPolicy
from cassetta.protocols.identity import Identity
from cassetta.protocols.limits import PolicyContext


def _ctx() -> PolicyContext:
    return PolicyContext(identity=Identity(label="x"))


def test_advertise_limits_with_all_caps_set() -> None:
    p = CoreLimitsPolicy(
        LimitsConfig(
            per_file_max=1,
            per_bundle_total_max=2,
            per_bundle_file_count_max=3,
            max_inline_size=4,
        )
    )
    assert p.advertise_limits(_ctx()) == {
        "per_file_max": 1,
        "per_bundle_total_max": 2,
        "per_bundle_file_count_max": 3,
        "max_inline_size": 4,
    }


def test_advertise_limits_defaults() -> None:
    p = CoreLimitsPolicy(LimitsConfig())
    assert p.advertise_limits(_ctx()) == {
        "per_file_max": None,
        "per_bundle_total_max": None,
        "per_bundle_file_count_max": 25,
        "max_inline_size": 102400,
    }


def test_advertise_limits_explicit_none_overrides() -> None:
    p = CoreLimitsPolicy(LimitsConfig(per_bundle_file_count_max=None, max_inline_size=None))
    ad = p.advertise_limits(_ctx())
    assert ad["per_bundle_file_count_max"] is None
    assert ad["max_inline_size"] is None


def test_ttls_custom_values() -> None:
    p = CoreLimitsPolicy(
        LimitsConfig(
            upload_token_ttl=10,
            download_claim_ttl=20,
            passive_gc_min_age=30,
            passive_gc_interval=40,
        )
    )
    assert p.ttls(_ctx()) == {
        "upload_token_ttl": 10,
        "download_claim_ttl": 20,
        "passive_gc_min_age": 30,
        "passive_gc_interval": 40,
    }


def test_ttls_defaults() -> None:
    p = CoreLimitsPolicy(LimitsConfig())
    assert p.ttls(_ctx()) == {
        "upload_token_ttl": 300,
        "download_claim_ttl": 300,
        "passive_gc_min_age": 3600,
        "passive_gc_interval": 600,
    }


def test_introspection_methods_are_sync() -> None:
    p = CoreLimitsPolicy(LimitsConfig())
    # Should not return coroutines
    ad = p.advertise_limits(_ctx())
    ttls = p.ttls(_ctx())
    assert not hasattr(ad, "__await__")
    assert not hasattr(ttls, "__await__")


def test_policy_context_extension_seam() -> None:
    """Cloud code may subclass PolicyContext with extra fields; core must ignore them."""
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class ExtendedContext:
        identity: Identity
        team: str = "t"
        extra: dict[str, str] = field(default_factory=dict)

    p = CoreLimitsPolicy(LimitsConfig(per_file_max=99))
    ad = p.advertise_limits(ExtendedContext(identity=Identity(label="y")))  # type: ignore[arg-type]
    assert ad["per_file_max"] == 99


# ---------------------------------------------------------------------------
# Single source of truth
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402

import pytest  # noqa: E402

from cassetta.limits import UploadManifest as _SharedUploadManifest  # noqa: E402
from cassetta.limits import check_manifest_against_limits  # noqa: E402
from cassetta.protocols.limits import ManifestFile  # noqa: E402


def _m(files: list[tuple[str, int]]) -> _SharedUploadManifest:
    entries: list[ManifestFile] = [{"name": name, "size": size, "mime": None} for name, size in files]
    return {"file_count": len(entries), "files": entries}


@pytest.mark.parametrize(
    "manifest,config",
    [
        # 1. Unlimited + tiny → inline.
        (
            _m([("a", 10)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 2. Unlimited + huge → inline (max_inline_size=None).
        (
            _m([("big", 10**9)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 3. per_file_max trips single file.
        (
            _m([("x", 2000)]),
            LimitsConfig(
                per_file_max=1000,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 4. per_file_max trips first of two (not the largest).
        (
            _m([("first", 1100), ("bigger", 2000)]),
            LimitsConfig(
                per_file_max=1000,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 5. per_bundle_total_max trips.
        (
            _m([("a", 400), ("b", 400), ("c", 400)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=1000,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 6. per_bundle_file_count_max trips.
        (
            _m([(f"f{i}", 10) for i in range(30)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=None,
                per_bundle_file_count_max=25,
                max_inline_size=None,
            ),
        ),
        # 7. inline selection (total below max_inline_size).
        (
            _m([("a", 300), ("b", 400)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=1000,
            ),
        ),
        # 8. batch selection (total above max_inline_size).
        (
            _m([("a", 700), ("b", 700)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=1000,
            ),
        ),
        # 9. total exactly at max_inline_size → inline (equality).
        (
            _m([("a", 500), ("b", 500)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=1000,
            ),
        ),
        # 10. per_file_max exactly at cap → inline.
        (
            _m([("a", 100)]),
            LimitsConfig(
                per_file_max=100,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 11. per_file_max exceeded by 1 byte.
        (
            _m([("a", 101)]),
            LimitsConfig(
                per_file_max=100,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 12. Two caps set; per_file_max wins ordering.
        (
            _m([("a", 2000), ("b", 10)]),
            LimitsConfig(
                per_file_max=500,
                per_bundle_total_max=2500,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 13. file_count cap fires before per_file_max.
        (
            _m([("a", 2000), ("b", 2000), ("c", 2000)]),
            LimitsConfig(
                per_file_max=100,
                per_bundle_total_max=None,
                per_bundle_file_count_max=2,
                max_inline_size=None,
            ),
        ),
        # 14. Many small files under all caps → inline.
        (
            _m([(f"f{i}", 10) for i in range(20)]),
            LimitsConfig(
                per_file_max=100,
                per_bundle_total_max=1000,
                per_bundle_file_count_max=25,
                max_inline_size=500,
            ),
        ),
        # 15. Many small files selecting batch.
        (
            _m([(f"f{i}", 100) for i in range(20)]),
            LimitsConfig(
                per_file_max=500,
                per_bundle_total_max=None,
                per_bundle_file_count_max=25,
                max_inline_size=500,
            ),
        ),
        # 16. per_bundle_total_max zero trips on any non-empty.
        (
            _m([("a", 1)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=0,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 17. All zeros → empty-ish manifest is inline.
        (
            _m([("empty", 0)]),
            LimitsConfig(
                per_file_max=0,
                per_bundle_total_max=0,
                per_bundle_file_count_max=1,
                max_inline_size=0,
            ),
        ),
        # 18. per_file_max=0 trips on any non-zero size.
        (
            _m([("a", 1)]),
            LimitsConfig(
                per_file_max=0,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=None,
            ),
        ),
        # 19. max_inline_size=0 → every non-empty bundle goes batch.
        (
            _m([("a", 1)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=None,
                per_bundle_file_count_max=None,
                max_inline_size=0,
            ),
        ),
        # 20. file_count_max=0 trips on any manifest.
        (
            _m([("a", 1)]),
            LimitsConfig(
                per_file_max=None,
                per_bundle_total_max=None,
                per_bundle_file_count_max=0,
                max_inline_size=None,
            ),
        ),
    ],
)
def test_evaluate_upload_matches_shared_helper(
    manifest: _SharedUploadManifest,
    config: LimitsConfig,
) -> None:
    """Policy and shared helper are bit-exact equal."""
    policy = CoreLimitsPolicy(config)
    ctx = _ctx()
    limits = policy.advertise_limits(ctx)
    helper_decision = check_manifest_against_limits(manifest, limits)
    policy_decision = asyncio.run(policy.evaluate_upload(ctx, manifest))
    assert helper_decision == policy_decision
