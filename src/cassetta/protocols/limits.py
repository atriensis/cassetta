"""Layer-1 protocol — pluggable upload/download limits policy.

Core defines the abstractions used by write entry points to decide
whether a payload is accepted inline, routed to batch transport, or
rejected for violating a hard cap. The default implementation
lives in :mod:`cassetta.defaults.default_limits`.

Layer-1 invariants: no vendor imports, no default values, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol, TypedDict, runtime_checkable

from cassetta.protocols.identity import Identity


@dataclass(frozen=True)
class PolicyContext:
    """Context passed into every LimitsPolicy method call.

    Core accesses only ``identity``. Cloud implementations may subclass
    or substitute a structurally-compatible type with additional fields.
    """

    identity: Identity


UploadMode = Literal["inline", "batch"]
DownloadMode = Literal["inline", "reference"]


class ManifestFile(TypedDict):
    name: str
    size: int
    mime: str | None


class UploadManifest(TypedDict):
    file_count: int
    files: list[ManifestFile]


class DownloadEntry(TypedDict):
    file_count: int
    total_size: int


class UploadDecision(TypedDict, total=False):
    """Return value of ``evaluate_upload``.

    On success, ``mode`` and ``reason`` are present; error-related keys
    are absent. On rejection, ``error`` / ``constraint`` / ``limit`` /
    ``observed`` are present; ``mode`` is absent. Consumers branch on
    ``"error" in decision``.
    """

    mode: UploadMode | list[UploadMode]
    reason: str | None
    error: Literal["cap_exceeded", "batch_required"]
    constraint: Literal[
        "per_file_max",
        "per_bundle_total_max",
        "per_bundle_file_count_max",
        "max_inline_size",
    ]
    limit: int | None
    observed: int


class DownloadDecision(TypedDict, total=False):
    mode: DownloadMode | list[DownloadMode]
    reason: str | None


class LimitsAdvertisement(TypedDict):
    per_file_max: int | None
    per_bundle_total_max: int | None
    per_bundle_file_count_max: int | None
    max_inline_size: int | None


class TTLSettings(TypedDict):
    upload_token_ttl: int
    download_claim_ttl: int
    passive_gc_min_age: int
    passive_gc_interval: int


@runtime_checkable
class LimitsPolicy(Protocol):
    """Strategy interface for upload/download mode selection, size
    caps, and TTLs.

    Implementations MUST be stateless with respect to the call — any
    state lives in injected dependencies (e.g. ``LimitsConfig`` wrapped
    by ``CoreLimitsPolicy``).
    """

    kind: ClassVar[str]

    async def evaluate_upload(
        self,
        ctx: PolicyContext,
        manifest: UploadManifest,
    ) -> UploadDecision: ...

    async def evaluate_download(
        self,
        ctx: PolicyContext,
        entry: DownloadEntry,
    ) -> DownloadDecision: ...

    def advertise_limits(self, ctx: PolicyContext) -> LimitsAdvertisement: ...

    def ttls(self, ctx: PolicyContext) -> TTLSettings: ...

    async def advertise_features(self, ctx: PolicyContext) -> list[str]: ...
