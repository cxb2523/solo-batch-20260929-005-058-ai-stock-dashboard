from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("stock_dash.cache")

SCHEMA_VERSION = 1

Fetcher = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass
class EntryStats:
    hits: int = 0
    expired: int = 0
    lock_waits: int = 0
    refresh_failures: int = 0
    last_refresh: float | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "expired": self.expired,
            "lock_waits": self.lock_waits,
            "refresh_failures": self.refresh_failures,
            "last_refresh": self.last_refresh,
            "last_error": self.last_error,
        }


@dataclass
class CacheEntry:
    key: str
    value: dict[str, Any]
    fetched_at: float
    ttl: float
    schema_version: int = SCHEMA_VERSION

    def is_fresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.schema_version == SCHEMA_VERSION and (now - self.fetched_at) < self.ttl

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "key": self.key,
            "value": self.value,
            "fetched_at": self.fetched_at,
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CacheEntry":
        return cls(
            key=data["key"],
            value=data["value"],
            fetched_at=float(data["fetched_at"]),
            ttl=float(data["ttl"]),
            schema_version=int(data.get("schema_version", 0)),
        )


@dataclass
class GetResult:
    key: str
    value: dict[str, Any]
    source: str  # "hit" | "fetched" | "stale_fallback"
    entry: CacheEntry


class CacheStore:
    """Disk-backed cache with per-key singleflight and observable stats.

    - Concurrent fetches for the same key are merged (singleflight).
    - Writes go through a temp file + os.replace for atomicity.
    - Entries carry a schema version; mismatches are treated as expired.
    - force=True bypasses the TTL and always penetrates to the upstream.
    - A failed refresh never overwrites an existing entry.
    """

    def __init__(self, cache_dir: str | os.PathLike[str], ttl: float):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = float(ttl)
        self._stats: dict[str, EntryStats] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard: asyncio.Lock | None = None

    # -- persistence ------------------------------------------------------

    def _path_for(self, key: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in key)
        return self.cache_dir / f"{safe}.json"

    def _write_entry(self, entry: CacheEntry) -> None:
        path = self._path_for(entry.key)
        fd, tmp_name = tempfile.mkstemp(dir=self.cache_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry.as_dict(), fh)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _read_entry(self, key: str) -> CacheEntry | None:
        path = self._path_for(key)
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                return CacheEntry.from_dict(json.load(fh))
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("discarding unreadable cache entry %s: %s", path, exc)
            return None

    # -- stats / locks ------------------------------------------------------

    def stats_for(self, key: str) -> EntryStats:
        return self._stats.setdefault(key, EntryStats())

    async def _lock_for(self, key: str) -> asyncio.Lock:
        if self._locks_guard is None:
            self._locks_guard = asyncio.Lock()
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    # -- public API ---------------------------------------------------------

    async def get(self, key: str, fetcher: Fetcher, force: bool = False) -> GetResult:
        """Return the value for key, fetching from upstream when needed.

        force=True penetrates to the upstream even inside the TTL window.
        A failed fetch never overwrites an existing entry; a stale entry is
        returned as a fallback instead.
        """
        stats = self.stats_for(key)
        lock = await self._lock_for(key)
        if lock.locked():
            stats.lock_waits += 1
        async with lock:
            entry = self._read_entry(key)
            fresh = entry is not None and entry.is_fresh()
            if entry is not None and not fresh:
                stats.expired += 1
            if fresh and not force:
                stats.hits += 1
                return GetResult(key=key, value=entry.value, source="hit", entry=entry)
            try:
                value = await fetcher(key)
            except Exception as exc:
                stats.refresh_failures += 1
                stats.last_error = str(exc)
                logger.warning("upstream fetch failed for %s: %s", key, exc)
                if entry is not None:
                    # Never clobber an existing entry on failure.
                    return GetResult(key=key, value=entry.value, source="stale_fallback", entry=entry)
                raise
            new_entry = CacheEntry(key=key, value=value, fetched_at=time.time(), ttl=self.ttl)
            await asyncio.to_thread(self._write_entry, new_entry)
            stats.last_refresh = new_entry.fetched_at
            return GetResult(key=key, value=value, source="fetched", entry=new_entry)

    def snapshot(self) -> list[dict[str, Any]]:
        """Per-key view for the /status page."""
        keys = set(self._stats)
        for path in self.cache_dir.glob("*.json"):
            keys.add(path.stem)
        rows = []
        now = time.time()
        for key in sorted(keys):
            entry = self._read_entry(key)
            stats = self.stats_for(key)
            rows.append({
                "key": key,
                "present": entry is not None,
                "fresh": entry.is_fresh(now) if entry else False,
                "fetched_at": entry.fetched_at if entry else None,
                "ttl": entry.ttl if entry else None,
                "schema_version": entry.schema_version if entry else None,
                **stats.as_dict(),
            })
        return rows
