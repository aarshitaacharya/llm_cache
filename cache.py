"""
llm_cache/cache.py  —  Shared LLM response cache with versioning & TTL.

This is the "distributed cache" that both workers talk to.
In production this would be Redis / Memcached; here it's an in-process
store with a deliberate sync_delay to make the stale-read window visible.

Key ideas:
  - Every entry carries a version counter and a write timestamp
  - Reads return the entry even when stale (AP behaviour — always available)
  - A background sync loop propagates writes from A→B after sync_delay seconds
  - Every read/write/sync is appended to an event log for timeline rendering
"""

from __future__ import annotations
import threading
import time
import hashlib
import dataclasses
from typing import Optional


# ── Data types ────────────────────────────────────────────────

@dataclasses.dataclass
class CacheEntry:
    prompt_hash: str       # key
    response:    str       # LLM output
    model:       str       # e.g. "gpt-4o"
    tokens:      int       # simulated token count
    version:     int       # increments on every update
    written_at:  float     # wall-clock time of the write
    written_by:  str       # worker id
    ttl:         float     # seconds until expiry (0 = never)

    def is_expired(self) -> bool:
        if self.ttl <= 0:
            return False
        return (time.time() - self.written_at) > self.ttl

    def age_ms(self) -> int:
        return int((time.time() - self.written_at) * 1000)


@dataclasses.dataclass
class CacheEvent:
    """Immutable record appended to the shared event log."""
    ts:        float   # absolute wall-clock time
    t:         float   # relative ms since sim start (filled in by Cache)
    worker:    str
    op:        str     # READ_HIT | READ_STALE | READ_MISS | WRITE | SYNC | EXPIRE
    key:       str
    version:   Optional[int]
    detail:    str


# ── The shared cache ──────────────────────────────────────────

class SharedLLMCache:
    """
    Two-tier cache simulating a 'remote' store (shared) and per-worker
    local views that diverge until sync_delay elapses.

    Architecture:
        Worker A  ──write──►  _store["A"]  ──sync after delay──►  _store["B"]
        Worker B  ──write──►  _store["B"]  ──sync after delay──►  _store["A"]

    Each worker only reads from its own shard; sync propagates across.
    This reproduces the stale-read window you get with:
      - Redis replication lag
      - CDN edge cache not yet invalidated
      - In-memory worker cache not yet refreshed
    """

    def __init__(self, sync_delay: float = 1.0, default_ttl: float = 0):
        self.sync_delay   = sync_delay    # seconds before a write reaches the other worker
        self.default_ttl  = default_ttl   # 0 = no expiry
        self._store: dict[str, dict[str, CacheEntry]] = {"A": {}, "B": {}}
        self._lock  = threading.Lock()
        self._events: list[CacheEvent] = []
        self._start_ts = time.time()

    # ── Public API ────────────────────────────────────────────

    def get(self, worker: str, prompt: str) -> Optional[CacheEntry]:
        key   = _hash(prompt)
        entry = self._store[worker].get(key)

        if entry is None:
            self._log(worker, "READ_MISS", key, None, f"prompt={_short(prompt)!r}")
            return None

        if entry.is_expired():
            with self._lock:
                self._store[worker].pop(key, None)
            self._log(worker, "EXPIRE", key, entry.version,
                      f"age={entry.age_ms()}ms ttl={entry.ttl}s")
            return None

        # Is this entry stale for this worker?
        # Case 1: other worker has a HIGHER version (classic replication lag)
        # Case 2: same version but DIFFERENT content (concurrent write conflict)
        other = _other(worker)
        other_entry = self._store[other].get(key)
        is_stale = other_entry is not None and (
            other_entry.version > entry.version
            or (other_entry.version == entry.version
                and other_entry.response != entry.response)
        )
        op = "READ_STALE" if is_stale else "READ_HIT"
        self._log(worker, op, key, entry.version,
                  f"v{entry.version} age={entry.age_ms()}ms" +
                  (f" (other=v{other_entry.version})" if is_stale else ""))
        return entry

    def set(self, worker: str, prompt: str, response: str,
            model: str = "gpt-4o-mini", tokens: int = 0,
            ttl: float | None = None) -> CacheEntry:
        key = _hash(prompt)
        ttl = ttl if ttl is not None else self.default_ttl

        with self._lock:
            prev    = self._store[worker].get(key)
            version = (prev.version + 1) if prev else 1
            entry   = CacheEntry(
                prompt_hash=key, response=response, model=model,
                tokens=tokens or len(response.split()),
                version=version, written_at=time.time(),
                written_by=worker, ttl=ttl,
            )
            self._store[worker][key] = entry

        self._log(worker, "WRITE", key, version,
                  f"v{version} model={model} tokens={entry.tokens}")

        # Schedule async propagation to the other worker
        threading.Timer(
            self.sync_delay,
            self._sync_entry,
            args=(worker, key, entry),
        ).start()

        return entry

    def invalidate(self, worker: str, prompt: str):
        """Explicitly remove a key from both shards immediately."""
        key = _hash(prompt)
        with self._lock:
            for shard in self._store.values():
                shard.pop(key, None)
        self._log(worker, "INVALIDATE", key, None, "both shards cleared")

    # ── Sync ──────────────────────────────────────────────────

    def _sync_entry(self, source_worker: str, key: str, entry: CacheEntry):
        """Called after sync_delay — propagate entry to the other shard."""
        target = _other(source_worker)
        with self._lock:
            existing = self._store[target].get(key)
            # Only overwrite if this version is newer
            if existing is None or entry.version > existing.version:
                self._store[target][key] = entry
                self._log(target, "SYNC", key, entry.version,
                          f"propagated from worker {source_worker} → {target} "
                          f"(delay={self.sync_delay}s)")
            else:
                self._log(target, "SYNC_SKIP", key, entry.version,
                          f"target already has v{existing.version} ≥ v{entry.version}")

    # ── Event log ─────────────────────────────────────────────

    def _log(self, worker: str, op: str, key: str,
             version: Optional[int], detail: str):
        self._events.append(CacheEvent(
            ts=time.time(),
            t=round((time.time() - self._start_ts) * 1000, 1),
            worker=worker, op=op, key=key,
            version=version, detail=detail,
        ))

    def events(self) -> list[CacheEvent]:
        return list(self._events)

    def stats(self, worker: str) -> dict:
        evts = self._events
        hits   = sum(1 for e in evts if e.worker == worker and e.op == "READ_HIT")
        stales = sum(1 for e in evts if e.worker == worker and e.op == "READ_STALE")
        misses = sum(1 for e in evts if e.worker == worker and e.op == "READ_MISS")
        total  = hits + stales + misses
        return {
            "hits": hits, "stales": stales, "misses": misses, "total": total,
            "hit_rate":   round(hits   / total * 100, 1) if total else 0,
            "stale_rate": round(stales / total * 100, 1) if total else 0,
        }


# ── Helpers ───────────────────────────────────────────────────

def _hash(prompt: str) -> str:
    return hashlib.sha1(prompt.encode()).hexdigest()[:10]

def _short(s: str, n: int = 30) -> str:
    return s if len(s) <= n else s[:n] + "…"

def _other(worker: str) -> str:
    return "B" if worker == "A" else "A"
