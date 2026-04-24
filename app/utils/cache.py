import asyncio
import time
from typing import Any, Awaitable, Callable


class TTLCache:
    """Simple in-memory TTL cache with async-safe get-or-set."""

    def __init__(self, default_ttl: float = 21600.0) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()
        self.default_ttl = default_ttl

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        expiry = time.monotonic() + (ttl if ttl is not None else self.default_ttl)
        self._data[key] = (expiry, value)

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

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
            lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            cached = self.get(key)
            if cached is not None:
                return cached
            value = await factory()
            self.set(key, value, ttl)
            return value
