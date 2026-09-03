"""Runtime Protocol conformance for the filesystem impl."""

from __future__ import annotations

from pathlib import Path

from cassetta.backends.filesystem.claim_storage import FilesystemClaimStorage
from cassetta.protocols.claim_storage import ClaimStorage


def test_filesystem_claim_storage_satisfies_protocol(tmp_path: Path) -> None:
    storage = FilesystemClaimStorage(tmp_path / ".claims")
    assert isinstance(storage, ClaimStorage)
