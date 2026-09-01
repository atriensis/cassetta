"""Default :class:`LimitsPolicy` implementation for the core reference backend.

``CoreLimitsPolicy`` wraps a :class:`LimitsConfig` (loaded from env
vars) and answers the four :class:`LimitsPolicy` questions with
straightforward arithmetic over the in-memory manifest. No I/O, no
state beyond the config passed at construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import ClassVar, Literal

from cassetta.config import LimitsConfig
from cassetta.limits import check_manifest_against_limits
from cassetta.protocols.limits import (
    DownloadDecision,
    DownloadEntry,
    LimitsAdvertisement,
    PolicyContext,
    TTLSettings,
    UploadDecision,
    UploadManifest,
)
from cassetta.structured_log import struct_log

logger = logging.getLogger("cassetta")

# Brief 531 FR-020 / FR-025: when ``LimitsPolicy.per_file_max is None``
# the upload pipeline still applies a 100 MiB ceiling. Closes the OOM
# vector flagged by SC-003 — undeclared (None) used to mean "no limit".
DEFAULT_PER_FILE_MAX = 100 * 1024 * 1024


def _format_reason(
    error: str,
    constraint: str,
    limit: int | None,
    observed: int,
    *,
    context: str | None = None,
) -> str:
    """Human-readable suffix for ``LimitsRejection``. Tests only assert the prefix."""
    if error == "batch_required":
        return (
            "bundle exceeds max inline size; batch transport is not "
            "available yet (see brief 514)"
        )
    if constraint == "per_bundle_file_count_max":
        return f"bundle has {observed} files; per_bundle_file_count_max is {limit}"
    if constraint == "per_bundle_total_max":
        return f"sum is {observed} bytes; per_bundle_total_max is {limit} bytes"
    if constraint == "per_file_max":
        who = context or "file"
        return f"{who} is {observed} bytes; per_file_max is {limit} bytes"
    return f"{constraint}: observed {observed}, limit {limit}"


@dataclass
class LimitsRejection(Exception):
    """Structured rejection raised by write entry points.

    Carries the four fields that land in the REST JSON body and
    the human-readable reason used as the MCP message suffix. The
    FastAPI exception handler (registered in :mod:`cassetta.app`)
    maps ``error="cap_exceeded"`` to HTTP 413 and
    ``error="batch_required"`` to HTTP 422.
    """

    error: Literal["cap_exceeded", "batch_required"]
    constraint: Literal[
        "per_file_max",
        "per_bundle_total_max",
        "per_bundle_file_count_max",
        "max_inline_size",
    ]
    limit: int | None
    observed: int
    reason: str

    def __str__(self) -> str:
        return f"{self.error}: {self.reason}"


class CoreLimitsPolicy:
    """Reference :class:`LimitsPolicy` implementation.

    Four methods are stubbed in Phase 2 and implemented in later
    phases:

    - ``evaluate_upload`` / ``evaluate_download`` — Phase 4 (US2).
    - ``advertise_limits`` / ``ttls`` — Phase 5 (US3).
    """

    kind: ClassVar[str] = "core"

    def __init__(self, config: LimitsConfig) -> None:
        self._config = config

    async def evaluate_upload(
        self, ctx: PolicyContext, manifest: UploadManifest,
    ) -> UploadDecision:
        # Delegates to the shared pure helper (FR-020) so server and
        # client pre-check paths share a single source of truth.
        return check_manifest_against_limits(manifest, self.advertise_limits(ctx))

    async def evaluate_download(
        self, ctx: PolicyContext, entry: DownloadEntry,
    ) -> DownloadDecision:
        total_size = int(entry.get("total_size", 0))
        inline_max = self._config.max_inline_size
        if inline_max is None or total_size <= inline_max:
            decision: DownloadDecision = {"mode": "inline", "reason": None}
        else:
            decision = {"mode": "reference", "reason": None}
        struct_log(
            logger, logging.DEBUG, "policy.download_decision",
            identity_label=ctx.identity.label,
            detail={
                "file_count": int(entry.get("file_count", 0)),
                "total_size": total_size,
                "decision": dict(decision),
            },
        )
        return decision

    def advertise_limits(self, ctx: PolicyContext) -> LimitsAdvertisement:
        return {
            "per_file_max": self._config.per_file_max,
            "per_bundle_total_max": self._config.per_bundle_total_max,
            "per_bundle_file_count_max": self._config.per_bundle_file_count_max,
            "max_inline_size": self._config.max_inline_size,
        }

    def ttls(self, ctx: PolicyContext) -> TTLSettings:
        return {
            "upload_token_ttl": self._config.upload_token_ttl,
            "download_claim_ttl": self._config.download_claim_ttl,
            "passive_gc_min_age": self._config.passive_gc_min_age,
            "passive_gc_interval": self._config.passive_gc_interval,
        }

    async def advertise_features(self, ctx: PolicyContext) -> list[str]:
        """Return the full canonical feature tuple unmodified.

        Brief 535 Fix 3 — core deployments have no per-identity feature
        gating. Cloud wraps this method to subtract features denied by
        the active ``AccessPolicy``.
        """
        from cassetta.capabilities import FEATURES

        return list(FEATURES)
