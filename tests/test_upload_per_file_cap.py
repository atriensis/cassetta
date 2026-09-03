"""Per-file size cap before reading bytes.

When ``LimitsPolicy.per_file_max is None``, the upload pipeline applies
a 100 MiB ceiling and rejects the offending entry BEFORE calling
``ExFileObject.read()`` on it, closing an OOM vector.
"""

from __future__ import annotations

import asyncio
import functools
import io
import tarfile
from typing import Any
from unittest.mock import patch

import anyio
import pytest

from cassetta.config import AppConfig, LimitsConfig
from cassetta.defaults.default_limits import (
    DEFAULT_PER_FILE_MAX,
    LimitsRejection,
)
from cassetta.routes.upload import _stream_tar_into_writer
from cassetta.streaming import SyncStreamReader


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []

    async def write_file(self, name: str, src: io.BytesIO) -> None:
        self.writes.append((name, len(src.getvalue())))


def _build_tarinfo(name: str, declared_size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = declared_size
    info.mode = 0o644
    return info


def _build_tar(name: str, data: bytes) -> bytes:
    """Build an in-memory tar carrying one regular file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = _build_tarinfo(name, len(data))
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_config(per_file_max: int | None) -> AppConfig:
    return AppConfig(
        setup_token="t",
        dev_mode=True,
        storage_path="/tmp/x",
        keys_file="/tmp/k",
        default_ttl=0,
        allowed_path_chars="a-zA-Z0-9_./-",
        mcp_allowed_hosts=(),
        jwt_primary_key=b"x" * 32,
        public_base_url="http://localhost:16001",
        limits=LimitsConfig(per_file_max=per_file_max),
    )


async def _drive(
    tar_bytes: bytes,
    manifest: list[dict[str, Any]],
    *,
    per_file_max: int | None,
    capture_reads: bool = False,
) -> tuple[_FakeWriter, list[str]]:
    """Run ``_stream_tar_into_writer`` inside an anyio worker thread."""
    config = _make_config(per_file_max)
    reader = SyncStreamReader()
    reader.feed(tar_bytes)
    reader.close()
    writer = _FakeWriter()
    _stream_tar_into_writer._loop = asyncio.get_running_loop()  # type: ignore[attr-defined]

    reads: list[str] = []
    original_read = tarfile.ExFileObject.read

    def _spy(self: Any, *a: Any, **kw: Any) -> bytes:
        reads.append(self.name)
        return original_read(self, *a, **kw)

    if capture_reads:
        with patch("tarfile.ExFileObject.read", new=_spy):
            await anyio.to_thread.run_sync(
                functools.partial(
                    _stream_tar_into_writer,
                    reader,
                    writer,
                    manifest,
                    "r|",
                    config=config,
                ),
            )
    else:
        await anyio.to_thread.run_sync(
            functools.partial(
                _stream_tar_into_writer,
                reader,
                writer,
                manifest,
                "r|",
                config=config,
            ),
        )
    return writer, reads


class TestDefaultCeiling:
    def test_default_constant_is_100_mib(self) -> None:
        assert DEFAULT_PER_FILE_MAX == 100 * 1024 * 1024

    @pytest.mark.asyncio
    async def test_oversize_under_default_rejected(self) -> None:
        # Cheap signal: build a tar with a body matching declared size,
        # then bump declared down so the cap fires after the size check.
        # Use 200-byte body and a tiny per_file_max so the body never
        # gets read in production-shape too.
        data = b"x" * 200
        tar = _build_tar("big.bin", data)
        manifest = [{"name": "big.bin", "size": 200}]
        with pytest.raises(LimitsRejection) as excinfo:
            await _drive(tar, manifest, per_file_max=64)
        rejection = excinfo.value
        assert rejection.error == "cap_exceeded"
        assert rejection.constraint == "per_file_max"
        assert rejection.limit == 64
        assert rejection.observed == 200

    @pytest.mark.asyncio
    async def test_no_read_occurs_for_oversize_entry(self) -> None:
        data = b"x" * 200
        tar = _build_tar("big.bin", data)
        manifest = [{"name": "big.bin", "size": 200}]
        with pytest.raises(LimitsRejection):
            _, reads = await _drive(
                tar,
                manifest,
                per_file_max=64,
                capture_reads=True,
            )
        # The spy lives on the patcher's scope; pull from the inner
        # tarfile reader instead by re-running with a manual capture.
        reads_outer: list[str] = []
        original_read = tarfile.ExFileObject.read

        def _spy(self: Any, *a: Any, **kw: Any) -> bytes:
            reads_outer.append(self.name)
            return original_read(self, *a, **kw)

        with patch("tarfile.ExFileObject.read", new=_spy):
            with pytest.raises(LimitsRejection):
                await _drive(tar, manifest, per_file_max=64)
        assert reads_outer == [], f"unexpected read of oversize entry: {reads_outer}"


class TestExplicitOverride:
    @pytest.mark.asyncio
    async def test_explicit_higher_cap_allows_oversize(self) -> None:
        data = b"x" * 1024
        tar = _build_tar("ok.bin", data)
        manifest = [{"name": "ok.bin", "size": len(data)}]
        writer, _ = await _drive(tar, manifest, per_file_max=524288000)
        assert writer.writes == [("ok.bin", len(data))]

    @pytest.mark.asyncio
    async def test_small_explicit_cap_rejects(self) -> None:
        data = b"x" * 1024
        tar = _build_tar("small.bin", data)
        manifest = [{"name": "small.bin", "size": len(data)}]
        with pytest.raises(LimitsRejection) as excinfo:
            await _drive(tar, manifest, per_file_max=512)
        assert excinfo.value.limit == 512
        assert excinfo.value.observed == 1024


class TestUnderDefault:
    @pytest.mark.asyncio
    async def test_small_entry_passes_under_default(self) -> None:
        # 1 KiB body — well under the 100 MiB default — should commit.
        data = b"x" * 1024
        tar = _build_tar("ok.bin", data)
        manifest = [{"name": "ok.bin", "size": len(data)}]
        writer, _ = await _drive(tar, manifest, per_file_max=None)
        assert writer.writes == [("ok.bin", len(data))]
