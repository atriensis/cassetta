"""Tests for default configuration (single-pod, no cloud deps)."""

import os

from cassetta.auth import FileKeyStore


class TestDefaultConfig:
    def test_file_keystore_works_without_cloud(self, storage_dir: str) -> None:
        """FileKeyStore works with just local filesystem, no cloud deps."""
        keys_file = os.path.join(storage_dir, "keys", ".cassetta-keys.json")
        store = FileKeyStore(keys_file=keys_file)
        assert store.is_healthy() is True
