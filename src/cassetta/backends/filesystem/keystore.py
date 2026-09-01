"""Filesystem-backed API key store.

Manages API keys with SHA-256 hashing and JSON file persistence.
"""

import hashlib
import json
import logging
import os
import secrets
from dataclasses import asdict
from datetime import UTC, datetime
from typing import ClassVar

from cassetta.auth.models import KEY_PREFIX, KEY_PREFIX_LEN, KeyInfo, KeyRecord

logger = logging.getLogger("cassetta.auth")


class FileKeyStore:
    """Manages API keys with SHA-256 hashing and JSON file persistence."""

    kind: ClassVar[str] = "filesystem"

    def __init__(self, keys_file: str) -> None:
        self._keys_file = keys_file
        self._keys: list[KeyRecord] = []
        self._setup_done = False
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._keys_file):
            with open(self._keys_file) as f:
                data = json.load(f)
            self._keys = [KeyRecord(**k) for k in data.get("keys", [])]
            self._setup_done = data.get("setup_done", False)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._keys_file) or ".", exist_ok=True)
        data = {
            "setup_done": self._setup_done,
            "keys": [asdict(k) for k in self._keys],
        }
        with open(self._keys_file, "w") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @staticmethod
    def _generate_key() -> str:
        return KEY_PREFIX + secrets.token_urlsafe(32)

    @property
    def setup_done(self) -> bool:
        return self._setup_done

    def is_healthy(self) -> bool:
        return True

    async def setup(self, label: str) -> tuple[str, KeyInfo]:
        """One-time setup: create the first API key."""
        if self._setup_done:
            raise ValueError("Setup already completed")
        self._setup_done = True
        raw_key, info = await self.create_key(label)
        logger.info("key.setup: label='%s', key_prefix='%s', outcome=created", label, info.key_prefix)
        return raw_key, info

    async def create_key(
        self, label: str, *, user_id: str | None = None,
    ) -> tuple[str, KeyInfo]:
        """Create a new API key with the given label.

        ``user_id`` is accepted to conform to the unified
        ``KeyStoreProtocol`` signature (Brief 535 Fix 5) and silently
        ignored — the filesystem backend is single-user and has no
        per-key owner concept.
        """
        for k in self._keys:
            if k.label == label and k.is_active:
                raise ValueError(f"Key with label '{label}' already exists")

        raw_key = self._generate_key()
        now = datetime.now(UTC)
        record = KeyRecord(
            label=label,
            key_hash=self._hash_key(raw_key),
            key_prefix=raw_key[: len(KEY_PREFIX) + KEY_PREFIX_LEN],
            created_at=now.isoformat(),
            is_active=True,
        )
        self._keys.append(record)
        self._save()
        logger.info("key.created: label='%s', key_prefix='%s'", label, record.key_prefix)
        return raw_key, KeyInfo(
            label=label,
            key_prefix=record.key_prefix,
            created_at=now,
            is_active=True,
        )

    async def validate(self, raw_key: str) -> KeyInfo | None:
        """Validate an API key. Returns KeyInfo if valid, None otherwise."""
        key_hash = self._hash_key(raw_key)
        for k in self._keys:
            if k.key_hash == key_hash and k.is_active:
                return KeyInfo(
                    label=k.label,
                    key_prefix=k.key_prefix,
                    created_at=datetime.fromisoformat(k.created_at),
                    is_active=k.is_active,
                )
        prefix = raw_key[:len(KEY_PREFIX) + KEY_PREFIX_LEN] if len(raw_key) > len(KEY_PREFIX) else raw_key
        logger.info("key.validate.failed: key_prefix='%s', reason=not_found", prefix)
        return None

    async def list_keys(self) -> list[KeyInfo]:
        """List all keys (active and revoked)."""
        return [
            KeyInfo(
                label=k.label,
                key_prefix=k.key_prefix,
                created_at=datetime.fromisoformat(k.created_at),
                is_active=k.is_active,
            )
            for k in self._keys
        ]

    async def revoke_key(self, label: str) -> None:
        """Revoke a key by label."""
        for k in self._keys:
            if k.label == label and k.is_active:
                k.is_active = False
                self._save()
                logger.info("key.revoked: label='%s'", label)
                return
        raise KeyError(f"Active key with label '{label}' not found")

    async def rotate_key(self, label: str) -> tuple[str, KeyInfo]:
        """Rotate a key: revoke old, create new with same label."""
        found = False
        for k in self._keys:
            if k.label == label and k.is_active:
                k.is_active = False
                found = True
                break
        if not found:
            raise KeyError(f"Active key with label '{label}' not found")

        raw_key, info = await self.create_key(label)
        logger.info("key.rotated: label='%s', new_key_prefix='%s'", label, info.key_prefix)
        return raw_key, info
