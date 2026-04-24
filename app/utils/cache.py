import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable


class TTLCache:
    """In-memory TTL cache with async-safe get-or-set and bounded size.

    When the number of entries exceeds max_size, the oldest (by insertion
    order) are evicted first, plus any entries found to be expired during
    the sweep. This keeps memory use bounded even with high-cardinality keys.
    """

    def __init__(self, default_ttl: float = 21600.0, max_size: int = 4096) -> None:
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()
        self._lock = asyncio.Lock()
        self.default_ttl = default_ttl
        self.max_size = max_size

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)  # mark as recently used
        return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        expiry = time.monotonic() + (ttl if ttl is not None else self.default_ttl)
        self._data[key] = (expiry, value)
        self._data.move_to_end(key)
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        if len(self._data) <= self.max_size:
            return
        # First pass: drop expired entries cheaply
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._data.items() if exp < now]
        for k in expired:
            self._data.pop(k, None)
        # Second pass: if still over, drop least-recently-used
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()
        self._locks.clear()

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Awaitable[Any]],
        ttl: float | None = None,
    ) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached

        async with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._locks.move_to_end(key)
            # Bound the locks dict too
            while len(self._locks) > self.max_size:
                self._locks.popitem(last=False)

        async with lock:
            cached = self.get(key)
            if cached is not None:
                return cached
            value = await factory()
            self.set(key, value, ttl)
            return value
