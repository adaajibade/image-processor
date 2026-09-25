from collections import OrderedDict
from collections.abc import Callable
from time import monotonic
from typing import Protocol


class Cacheable(Protocol):
    @property
    def data(self) -> bytes: ...


class ByteCache[T: Cacheable]:
    """Keep recently used results within byte, entry-count, and expiry limits."""

    def __init__(
        self, max_bytes: int, max_entries: int, clock: Callable[[], float] = monotonic
    ) -> None:
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.clock = clock
        self._entries: OrderedDict[str, tuple[T, float]] = OrderedDict()
        self._size_bytes = 0

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= self.clock():
            self._delete(key)
            return None
        self._entries.move_to_end(key)
        return value

    def set(self, key: str, value: T, expires_at: float) -> None:
        self._delete(key)
        if not self.max_entries or len(value.data) > self.max_bytes or expires_at <= self.clock():
            return
        while (
            self._size_bytes + len(value.data) > self.max_bytes
            or len(self._entries) >= self.max_entries
        ):
            self._delete(next(iter(self._entries)))
        self._entries[key] = value, expires_at
        self._size_bytes += len(value.data)

    def _delete(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry:
            self._size_bytes -= len(entry[0].data)
