"""Async-to-sync stream bridge for streaming tar parsing.

:class:`SyncStreamReader` is a bounded-queue reader with a synchronous
``read(n)`` interface suitable for stdlib ``tarfile`` in pipe mode
(``r|`` / ``r|gz``). An async producer (FastAPI request body loop) feeds
chunks via :meth:`feed`; a thread-bound consumer calls :meth:`read`
synchronously. Bounded queue size gives natural TCP backpressure when the
consumer is slower than the producer.
"""

from __future__ import annotations

import queue

_DEFAULT_QUEUE_SIZE = 8
_EOF_SENTINEL: None = None


class SyncStreamReader:
    """Queue-backed bridge: async producer ⇒ sync ``read(n)`` consumer.

    Thread-safe for exactly one producer + one consumer. ``feed`` blocks
    when the bounded queue is full; ``read`` blocks when it is empty and
    not closed.
    """

    def __init__(self, queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_size)
        self._pending: bytearray = bytearray()
        self._eof_seen: bool = False

    def feed(self, chunk: bytes) -> None:
        """Enqueue ``chunk``. Blocks if the queue is full (backpressure)."""
        if not chunk:
            return
        self._queue.put(chunk)

    def close(self) -> None:
        """Signal end-of-stream to the consumer. Idempotent."""
        self._queue.put(_EOF_SENTINEL)

    def read(self, n: int = -1) -> bytes:
        """Read up to ``n`` bytes (or until EOF if ``n < 0``).

        ``n == 0`` returns ``b""`` without consuming the queue. After EOF is
        seen, further calls return ``b""``.
        """
        if n == 0:
            return b""

        buf = self._pending
        while True:
            have = len(buf)
            if n > 0 and have >= n:
                result = bytes(buf[:n])
                self._pending = bytearray(buf[n:])
                return result
            if self._eof_seen:
                # Drain whatever is buffered. Subsequent reads will see
                # an empty buffer and return b"".
                result = bytes(buf)
                self._pending = bytearray()
                return result
            chunk = self._queue.get()
            if chunk is None:
                self._eof_seen = True
                continue
            buf.extend(chunk)
