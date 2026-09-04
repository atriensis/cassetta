"""Failing-first tests for ``cassetta.streaming.SyncStreamReader``.

Covers:
- ``read(n)`` accumulates across multiple ``feed`` calls.
- Partial reads after ``close`` return whatever is buffered, then EOF.
- Backpressure: ``feed`` blocks when the bounded queue is full.
- ``close()`` transitions reads to ``b""``.
- ``read(-1)`` drains to EOF.
"""

from __future__ import annotations

import threading
import time

import pytest

from cassetta.streaming import SyncStreamReader


def test_read_accumulates_across_feeds() -> None:
    r = SyncStreamReader()
    r.feed(b"hello ")
    r.feed(b"world")
    r.close()
    assert r.read(11) == b"hello world"


def test_partial_read_after_close_then_eof() -> None:
    r = SyncStreamReader()
    r.feed(b"abc")
    r.close()
    # Ask for more than available — should return the 3 buffered bytes, then EOF.
    assert r.read(10) == b"abc"
    assert r.read(1) == b""


def test_close_without_any_feed_returns_empty() -> None:
    r = SyncStreamReader()
    r.close()
    assert r.read(100) == b""


def test_read_negative_drains_to_eof() -> None:
    r = SyncStreamReader()
    for chunk in (b"a", b"bb", b"ccc"):
        r.feed(chunk)
    r.close()
    assert r.read(-1) == b"abbccc"


def test_read_returns_exact_requested_size_when_available() -> None:
    r = SyncStreamReader()
    r.feed(b"0123456789")
    assert r.read(4) == b"0123"
    assert r.read(3) == b"456"
    r.close()
    assert r.read(-1) == b"789"


def test_backpressure_feed_blocks_when_queue_full() -> None:
    """With maxsize 8, nine feeds without a concurrent reader must block the 9th."""
    r = SyncStreamReader()
    for _ in range(8):
        r.feed(b"x")
    blocked: list[bool] = [False]

    def produce_ninth() -> None:
        # This call should block because the queue holds 8 items.
        r.feed(b"y")

    t = threading.Thread(target=produce_ninth, daemon=True)
    t.start()
    t.join(timeout=0.1)
    # Thread still alive ⇒ feed is blocking.
    blocked[0] = t.is_alive()

    # Drain the queue by reading — this unblocks the producer.
    assert r.read(8) == b"x" * 8
    # Let the ninth producer finish.
    t.join(timeout=1.0)
    assert not t.is_alive()
    assert blocked[0] is True
    # Drain remaining byte.
    r.close()
    assert r.read(-1) == b"y"


@pytest.mark.parametrize("chunk_sizes", [[1, 2, 3, 4], [5, 5], [10]])
def test_read_honours_all_feed_chunk_shapes(chunk_sizes: list[int]) -> None:
    r = SyncStreamReader()
    expected = bytearray()
    for i, n in enumerate(chunk_sizes):
        chunk = bytes([i + 1]) * n
        expected.extend(chunk)
        r.feed(chunk)
    r.close()
    assert r.read(-1) == bytes(expected)


def test_read_zero_returns_empty_without_blocking() -> None:
    r = SyncStreamReader()
    r.feed(b"abc")
    start = time.monotonic()
    assert r.read(0) == b""
    assert time.monotonic() - start < 0.5
    r.close()
