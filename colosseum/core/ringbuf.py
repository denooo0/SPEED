"""Fixed-capacity O(1) ring buffer.

At 1 Hz forever, unbounded lists are a memory leak with a slow fuse. Every
rolling window in this engine is bounded at construction. Nothing grows without
a ceiling -- that is a hard architectural rule, not an optimization.
"""
from __future__ import annotations

from typing import Generic, Iterator, List, Optional, TypeVar

T = TypeVar("T")


class Ring(Generic[T]):
    __slots__ = ("_buf", "_cap", "_n", "_head")

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._buf: List[Optional[T]] = [None] * capacity
        self._cap = capacity
        self._n = 0      # total ever pushed (monotonic)
        self._head = 0   # next write slot

    def push(self, item: T) -> None:
        self._buf[self._head] = item
        self._head = (self._head + 1) % self._cap
        self._n += 1

    def __len__(self) -> int:
        return min(self._n, self._cap)

    @property
    def full(self) -> bool:
        return self._n >= self._cap

    @property
    def total_pushed(self) -> int:
        return self._n

    def __getitem__(self, i: int) -> T:
        """i=0 is oldest retained, i=-1 is newest. Standard sequence semantics."""
        size = len(self)
        if size == 0:
            raise IndexError("empty ring")
        if i < 0:
            i += size
        if not 0 <= i < size:
            raise IndexError(f"index {i} out of range for size {size}")
        start = (self._head - size) % self._cap
        return self._buf[(start + i) % self._cap]  # type: ignore[return-value]

    def last(self, k: int = 1) -> List[T]:
        """Most recent k items, oldest-first. The common access pattern."""
        size = len(self)
        k = min(k, size)
        return [self[size - k + j] for j in range(k)]

    def __iter__(self) -> Iterator[T]:
        for i in range(len(self)):
            yield self[i]

    def to_list(self) -> List[T]:
        return list(self)

    def clear(self) -> None:
        self._buf = [None] * self._cap
        self._n = 0
        self._head = 0
