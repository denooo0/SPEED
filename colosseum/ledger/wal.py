"""Crash-safe append-only write-ahead log.

The engine's entire self-healing story rests on one assumption: THE LOG IS
TRUE. If a power cut can leave a half-written record that silently parses as
valid, every downstream guarantee collapses.

Record framing:

    [ magic u32 ][ length u32 ][ crc32 u32 ][ payload bytes ]

On open, the log is scanned forward. The first record that fails magic, length
sanity, or CRC truncates the file at that point -- a torn tail from a crash is
discarded rather than trusted. Because every write is content-addressed and
idempotent, re-running recovery re-appends the lost record harmlessly.

fsync policy is explicit: `durable=True` fsyncs every record (slow, absolute),
otherwise fsync on a cadence. Trading decisions are journaled durably; telemetry
is not. That choice is made per-log, in the open, rather than assumed.
"""
from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path
from typing import Iterator, Optional

MAGIC = 0xC0175E17  # "COLOSEum" leetspeak; any fixed sentinel works
HEADER = struct.Struct("<III")   # magic, length, crc32
MAX_RECORD = 32 * 1024 * 1024


class WalCorruption(Exception):
    pass


class WriteAheadLog:
    def __init__(self, path: str | Path, durable: bool = True,
                 fsync_every: int = 1):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.durable = durable
        self.fsync_every = max(1, fsync_every)
        self._since_sync = 0
        self.truncated_bytes = 0
        self.records_recovered = 0
        self._recover()
        self._f = open(self.path, "ab", buffering=0)

    # ---- recovery --------------------------------------------------------

    def _recover(self) -> None:
        """Scan forward; truncate at the first damaged frame."""
        if not self.path.exists():
            self.path.touch()
            return
        good_end, count = 0, 0
        with open(self.path, "rb") as f:
            while True:
                head = f.read(HEADER.size)
                if len(head) < HEADER.size:
                    break
                magic, length, crc = HEADER.unpack(head)
                if magic != MAGIC or length == 0 or length > MAX_RECORD:
                    break
                payload = f.read(length)
                if len(payload) < length:
                    break                       # torn tail
                if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                    break                       # bit rot / partial flush
                good_end = f.tell()
                count += 1
        size = self.path.stat().st_size
        if good_end < size:
            self.truncated_bytes = size - good_end
            with open(self.path, "r+b") as f:
                f.truncate(good_end)
        self.records_recovered = count

    # ---- write / read ----------------------------------------------------

    def append(self, payload: bytes) -> int:
        if not payload:
            raise ValueError("empty payload")
        if len(payload) > MAX_RECORD:
            raise ValueError("record too large")
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        self._f.write(HEADER.pack(MAGIC, len(payload), crc))
        self._f.write(payload)
        self._since_sync += 1
        if self.durable and self._since_sync >= self.fsync_every:
            self.flush()
        return len(payload) + HEADER.size

    def flush(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())
        self._since_sync = 0

    def read_all(self) -> Iterator[bytes]:
        with open(self.path, "rb") as f:
            while True:
                head = f.read(HEADER.size)
                if len(head) < HEADER.size:
                    return
                magic, length, crc = HEADER.unpack(head)
                if magic != MAGIC:
                    raise WalCorruption(f"bad magic at {f.tell()-HEADER.size}")
                payload = f.read(length)
                if len(payload) < length:
                    return
                if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                    raise WalCorruption(f"crc mismatch at {f.tell()-length}")
                yield payload

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._f.close()

    def __enter__(self) -> "WriteAheadLog":
        return self

    def __exit__(self, *a) -> None:
        self.close()

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0
