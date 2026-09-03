"""Public limits utilities.

Single source of truth for manifest-vs-limits arithmetic, shared
between ``CoreLimitsPolicy.evaluate_upload`` on the server side and
any MCP client that pre-checks a payload before calling
``cassetta_send_init``.

Public API::

    from cassetta.limits import (
        check_manifest_against_limits,
        UploadManifest,
        ManifestFile,
        LimitsAdvertisement,
        UploadDecision,
    )

The helper is pure, synchronous, and side-effect-free (no I/O, no
logging, no input mutation). Re-exports the TypedDicts from
:mod:`cassetta.protocols.limits` so callers don't need to reach into
the protocol namespace.

See ``specs/516-advertise-limits-handshake/contracts/check-manifest-against-limits.md``
for the full contract.
"""

from __future__ import annotations

from cassetta.protocols.limits import (
    LimitsAdvertisement,
    ManifestFile,
    UploadDecision,
    UploadManifest,
)

__all__ = [
    "LimitsAdvertisement",
    "ManifestFile",
    "UploadDecision",
    "UploadManifest",
    "check_manifest_against_limits",
]


def check_manifest_against_limits(
    manifest: UploadManifest,
    limits: LimitsAdvertisement,
) -> UploadDecision:
    """Evaluate a manifest against a limits advertisement.

    Pure arithmetic, deterministic, no I/O. The ordering of checks is
    fixed and matches the server's ``CoreLimitsPolicy.evaluate_upload``
    exactly — the policy delegates to this helper.

    Args:
        manifest: Upload manifest (``file_count`` + ``files``).
        limits: Four-field limits block. ``None`` in any field means
            "no limit on this axis".

    Returns:
        An :class:`UploadDecision`. On acceptance:
        ``{"mode": "inline" | "batch", "reason": None}``. On rejection:
        ``{"error": "cap_exceeded" | "batch_required",
           "constraint": "...", "limit": int | None, "observed": int}``.
    """
    files = manifest.get("files", [])
    file_count = int(manifest.get("file_count", len(files)))

    count_max = limits["per_bundle_file_count_max"]
    if count_max is not None and file_count > count_max:
        return {
            "error": "cap_exceeded",
            "constraint": "per_bundle_file_count_max",
            "limit": count_max,
            "observed": file_count,
        }

    per_file = limits["per_file_max"]
    if per_file is not None:
        for f in files:
            size = int(f.get("size", 0))
            if size > per_file:
                return {
                    "error": "cap_exceeded",
                    "constraint": "per_file_max",
                    "limit": per_file,
                    "observed": size,
                }

    total_size = sum(int(f.get("size", 0)) for f in files)
    total_max = limits["per_bundle_total_max"]
    if total_max is not None and total_size > total_max:
        return {
            "error": "cap_exceeded",
            "constraint": "per_bundle_total_max",
            "limit": total_max,
            "observed": total_size,
        }

    inline_max = limits["max_inline_size"]
    if inline_max is None or total_size <= inline_max:
        return {"mode": "inline", "reason": None}
    return {"mode": "batch", "reason": None}
