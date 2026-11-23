"""The shared JSON cache: Redis when configured, otherwise an in-process memory cache.

Redis entries use the reference implementation's distributed-cache layout (a hash with ``absexp``, ``sldexp``
and ``data`` fields under the ``healthcare:`` prefix), so several backends can share one Redis and
invalidate each other's entries. Any cache failure degrades to a database lookup.
"""

import logging
import time
from typing import Final
from urllib.parse import quote

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

_LOGGER = logging.getLogger("healthcare.cache")
PREFIX: Final = "healthcare:"
_TICKS_AT_UNIX_EPOCH: Final = 621_355_968_000_000_000
_SET_SCRIPT: Final = """
redis.call('HSET', KEYS[1], 'absexp', ARGV[1], 'sldexp', '-1', 'data', ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""


def redis_url(connection_string: str) -> str:
    """Convert a StackExchange-style connection string (``host:port,password=...,ssl=true``) into a URL."""
    value = connection_string.strip()
    if value.startswith(("redis://", "rediss://")):
        return value
    endpoint, *options = [part.strip() for part in value.split(",")]
    user = password = database = ""
    tls = False
    for option in options:
        key, separator, item = option.partition("=")
        if not separator:
            continue
        match key.strip().lower():
            case "password":
                password = item.strip()
            case "user":
                user = item.strip()
            case "ssl":
                tls = item.strip().lower() == "true"
            case "defaultdatabase":
                database = item.strip()
    if ":" not in endpoint:
        endpoint += ":6379"
    credentials = f"{quote(user, safe='')}:{quote(password, safe='')}@" if password else ""
    return f"{'rediss' if tls else 'redis'}://{credentials}{endpoint}{'/' + database if database else ''}"


class RedisCache:
    """The Redis-backed cache shared with the other backends."""

    def __init__(self, connection_string: str) -> None:
        """Create a lazily connecting client."""
        self._client: Redis = Redis.from_url(
            redis_url(connection_string),
            socket_timeout=2,
            socket_connect_timeout=2,
            max_connections=64,
            decode_responses=False,
            # RESP2 works with every server version; redis-py 8 would otherwise negotiate RESP3 with
            # HELLO 3, which Redis 5 (still common on Windows hosts) rejects.
            protocol=2,
        )
        self._script: AsyncScript = self._client.register_script(_SET_SCRIPT)
        self._last_warning = 0.0

    def _warn(self, message: str, error: Exception) -> None:
        """Log a cache failure at most once every ten seconds so an outage does not flood the logs."""
        now = time.monotonic()
        if now - self._last_warning > 10:
            self._last_warning = now
            _LOGGER.warning(message, extra={"errorType": type(error).__name__})

    async def get(self, key: str) -> str | None:
        """Return the data field of the entry."""
        try:
            value = await self._client.hget(PREFIX + key, "data")
        except Exception as error:
            self._warn("Cache read failed; falling back to the database", error)
            return None
        if value is None:
            return None
        return value if isinstance(value, str) else value.decode("utf-8")

    async def _write(self, key: str, value: str, ttl_seconds: float) -> None:
        """Run the set script with an absolute expiry in .NET ticks."""
        ttl_ms = int(ttl_seconds * 1000)
        absolute = (int(time.time() * 1000) + ttl_ms) * 10_000 + _TICKS_AT_UNIX_EPOCH
        await self._script(keys=[PREFIX + key], args=[str(absolute), str(ttl_ms), value.encode("utf-8")])

    async def set(self, key: str, value: str, ttl_seconds: float) -> None:
        """Store the value with an absolute expiry."""
        try:
            await self._write(key, value, ttl_seconds)
        except Exception as error:
            self._warn("Cache write failed", error)

    async def remove(self, key: str) -> None:
        """Delete the entry; failures are logged because the entry expires on its own."""
        try:
            await self._client.delete(PREFIX + key)
        except Exception as error:
            self._warn("Cache invalidation failed; the entry expires shortly", error)

    async def probe(self) -> bool:
        """Write and remove a probe entry."""
        try:
            await self._write("health:probe", "1", 10)
            await self._client.delete(PREFIX + "health:probe")
        except Exception:
            return False
        return True

    async def close(self) -> None:
        """Close the client."""
        await self._client.aclose()


class MemoryCache:
    """The in-process cache used without Redis; entries are per process, which suits a single instance."""

    def __init__(self) -> None:
        """Create an empty cache."""
        self._entries: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> str | None:
        """Return an unexpired value."""
        entry = self._entries.get(key)
        if entry is None or entry[0] <= time.monotonic():
            return None
        return entry[1]

    async def set(self, key: str, value: str, ttl_seconds: float) -> None:
        """Store a value; expired entries are pruned when the cache grows large."""
        now = time.monotonic()
        if len(self._entries) > 10_000:
            self._entries = {name: entry for name, entry in self._entries.items() if entry[0] > now}
        self._entries[key] = (now + ttl_seconds, value)

    async def remove(self, key: str) -> None:
        """Delete a value."""
        self._entries.pop(key, None)

    async def probe(self) -> bool:
        """Report success; the memory cache cannot fail."""
        return True

    async def close(self) -> None:
        """Release nothing; the memory cache holds no connections."""
