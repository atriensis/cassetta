"""Skeleton tests for CoreLimitsPolicy + LimitsRejection (brief 513)."""

from __future__ import annotations

from cassetta.config import LimitsConfig
from cassetta.defaults.default_limits import CoreLimitsPolicy, LimitsRejection
from cassetta.protocols.limits import LimitsPolicy


def test_core_limits_policy_satisfies_protocol() -> None:
    policy = CoreLimitsPolicy(LimitsConfig())
    assert isinstance(policy, LimitsPolicy)


def test_limits_rejection_str_format() -> None:
    exc = LimitsRejection(
        error="cap_exceeded",
        constraint="per_file_max",
        limit=10,
        observed=20,
        reason="x is 20",
    )
    assert str(exc) == "cap_exceeded: x is 20"
