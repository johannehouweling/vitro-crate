"""Self-contained throttle + cache helpers for compound resolution (Issue #252).

``resolve_compound`` was very slow for some compounds (30-66s each) in a real
run: under a concurrent burst, a single call fanned out to up to SIX PubChem
round-trips (name->JSON + synonyms for the lookup, then a fresh re-resolution of
the *same* compound for each of the CAS and PubChem-CID verifications), and a 429
storm multiplied the retry/backoff across all of them.

This module holds the in-process levers that close that gap, kept here so they
stay transport-agnostic and reusable (the MCP server can back its own resolution
on them too):

* :func:`normalize_compound_name` — a stable cache key (strip + collapse
  whitespace + casefold) so ``"Rifampicin"``, ``"rifampicin "`` and
  ``"RIFAMPICIN"`` share one entry. PubChem name lookups are case-insensitive, so
  this never changes *which* compound is resolved — only how often we re-fetch it.
* :class:`_CompoundCache` — a thread-safe in-process map from normalized name (or
  a resolved CAS / ``CID <cid>`` alias key) to the ``{found, data, error}`` lookup
  result. Repeated names — and the CAS/CID *verify* re-resolutions of an
  already-resolved compound — become instant, with no network at all.
* :class:`_ResolveConcurrency` — a bounded client-side admission gate so a burst
  of concurrent ``resolve_compound`` calls does not all hit PubChem at once and
  trip its rate limiter into a 429 storm (the per-host throttle in
  ``lookups._http`` spaces requests; this caps how many compounds resolve at
  once).
* :func:`run_with_timeout` — runs a callable in a worker thread and returns
  ``(ok, value)``, signalling a graceful timeout instead of letting one slow
  compound hang the whole run ~60s.

Nothing here fabricates data: the cache only stores results that came from the
authority, and the alias keys point at that same authoritative record, so D5
(identifiers from the authority, verified) is preserved.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from typing import Any

# Default upper bound on how many compounds resolve concurrently. Sized to sit
# comfortably under PubChem's documented ~5 req/s budget once the per-host
# throttle (lookups._http) spaces the requests each resolution issues.
DEFAULT_RESOLVE_CONCURRENCY = 4

# Default per-compound wall-clock budget (seconds). A resolution that exceeds it
# returns a graceful partial/empty result rather than hanging on a stuck network
# round-trip. Generous enough for a healthy multi-round-trip lookup, far below the
# 30-66s pathological stalls Issue #252 reports.
DEFAULT_RESOLVE_TIMEOUT = 20.0


def normalize_compound_name(name: str) -> str:
    """Return a stable cache key for a compound name.

    Strips surrounding whitespace, collapses internal runs of whitespace to a
    single space, and casefolds. PubChem name resolution is case-insensitive, so
    this only affects cache-key identity, never the resolved compound.
    """
    return " ".join(str(name or "").split()).casefold()


class _CompoundCache:
    """Thread-safe in-process cache of compound lookup results.

    Keyed by normalized name and by resolved identifier *alias* keys (the bare
    CAS and the ``CID <cid>`` form ``verify_identifier`` re-resolves with), all
    pointing at the same authoritative ``{found, data, error}`` record.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        """Return the cached result for ``key`` (a copy), or ``None``."""
        with self._lock:
            hit = self._store.get(key)
            return dict(hit) if hit is not None else None

    def put(self, key: str, result: dict[str, Any]) -> None:
        """Cache ``result`` under ``key`` (stored as a shallow copy)."""
        with self._lock:
            self._store[key] = dict(result)

    def clear(self) -> None:
        """Forget all cached results (used by tests and between runs)."""
        with self._lock:
            self._store.clear()


class _ResolveConcurrency:
    """Bounded admission gate for concurrent compound resolutions.

    ``slot()`` is a context manager that blocks until a slot is free, so at most
    ``limit`` resolutions run their network round-trips at once. This stops a
    burst of concurrent ``resolve_compound`` calls from all hitting PubChem
    simultaneously and tripping its rate limiter into a 429 storm.
    """

    def __init__(self, limit: int = DEFAULT_RESOLVE_CONCURRENCY) -> None:
        self._limit = max(1, int(limit))
        self._sema = threading.BoundedSemaphore(self._limit)

    @property
    def limit(self) -> int:
        """Maximum number of concurrent resolutions admitted."""
        return self._limit

    @contextlib.contextmanager
    def slot(self) -> Iterator[None]:
        """Acquire a concurrency slot for the duration of the ``with`` block."""
        self._sema.acquire()
        try:
            yield
        finally:
            self._sema.release()

    def reset(self) -> None:
        """Reset the gate to a fresh, fully-available semaphore (tests)."""
        self._sema = threading.BoundedSemaphore(self._limit)


def run_with_timeout(
    func: Callable[[], Any], timeout: float
) -> tuple[bool, Any]:
    """Run ``func()`` in a worker thread, bounding it to ``timeout`` seconds.

    Returns ``(True, value)`` if it completes in time, ``(False, None)`` if it
    exceeds the budget (the worker is left to finish in the background — it is a
    daemon thread and only ever performs an idempotent, side-effect-free network
    read). A non-positive ``timeout`` means "no bound": run inline and return its
    value.

    Args:
        func: A zero-argument callable (close over its inputs).
        timeout: Wall-clock budget in seconds; ``<= 0`` disables the bound.

    Returns:
        ``(ok, value)`` — ``ok`` is False only on a timeout.
    """
    if timeout is None or timeout <= 0:
        return True, func()

    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # surface in the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return False, None
    if "error" in box:
        raise box["error"]
    return True, box.get("value")


# Process-wide singletons shared by resolve_compound (and reusable by callers
# that resolve compounds through their own path).
compound_cache = _CompoundCache()
resolve_concurrency = _ResolveConcurrency()
