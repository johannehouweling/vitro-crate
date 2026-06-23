"""Tests for the per-host rate limiter in the shared HTTP helper (#62).

The deliberate ``time.sleep(0.1)`` politeness delays that used to live in each
lookup client are replaced by a single thread-safe, per-host throttle in
``lookups._http``. This keeps the project polite to third-party APIs even when
independent requests are issued concurrently (e.g. AOP-Wiki event details).
"""

from __future__ import annotations

import threading
import time

import pytest

from lookups import _http


@pytest.fixture(autouse=True)
def _reset_throttle():
    """Reset the per-host throttle state before and after each test."""
    _http.reset_host_throttle()
    yield
    _http.reset_host_throttle()


class TestHostRateLimiter:
    """`_HostRateLimiter.wait` spaces requests to the same host."""

    def test_sequential_calls_to_same_host_are_spaced(self):
        """Two back-to-back acquisitions for one host are >= min_interval apart."""
        limiter = _http._HostRateLimiter(min_interval=0.1)
        start = time.monotonic()
        limiter.wait("aopwiki.org")
        limiter.wait("aopwiki.org")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.1, f"second call should wait, elapsed={elapsed:.3f}s"

    def test_different_hosts_are_independent(self):
        """A request to a different host is not delayed by another host."""
        limiter = _http._HostRateLimiter(min_interval=0.5)
        start = time.monotonic()
        limiter.wait("aopwiki.org")
        limiter.wait("pubchem.ncbi.nlm.nih.gov")
        elapsed = time.monotonic() - start
        # Two different hosts: neither blocks the other, so well under one
        # min_interval of total wait.
        assert elapsed < 0.5, f"distinct hosts must not block each other, elapsed={elapsed:.3f}s"

    def test_concurrent_calls_do_not_exceed_rate_cap(self):
        """N concurrent acquisitions for one host serialize to >= (N-1)*interval."""
        limiter = _http._HostRateLimiter(min_interval=0.05)
        n = 6
        barrier = threading.Barrier(n)
        timestamps: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()  # release all threads at once
            limiter.wait("aopwiki.org")
            with lock:
                timestamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        timestamps.sort()
        # Minimum spacing between any two consecutive grants respects the cap.
        for earlier, later in zip(timestamps, timestamps[1:]):
            gap = later - earlier
            assert gap >= 0.05 - 0.01, f"consecutive grants too close: {gap:.3f}s"
        # And the whole batch took at least (n-1) intervals — i.e. the rate cap
        # was never exceeded by parallelism.
        total = timestamps[-1] - timestamps[0]
        assert total >= (n - 1) * 0.05 - 0.02, f"batch finished too fast: {total:.3f}s"
