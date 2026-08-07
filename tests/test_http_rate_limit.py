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


class _FrozenClock:
    """A stand-in for the ``time`` module with a clock that never advances.

    ``_HostRateLimiter`` reads ``time.monotonic()`` and calls ``time.sleep()``,
    both through the module object, so swapping the module out captures the
    limiter's decisions exactly. Freezing rather than advancing is deliberate:
    the reservation arithmetic is what the cap *is*, and it is computed under
    the lock, so a frozen clock exercises it fully while removing every
    scheduler-dependent quantity from the assertions.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)


@pytest.fixture(autouse=True)
def _reset_throttle():
    """Reset the per-host throttle state before and after each test."""
    _http.reset_host_throttle()
    yield
    _http.reset_host_throttle()


class TestHostRateLimiter:
    """`_HostRateLimiter.wait` spaces requests to the same host."""

    def test_sequential_calls_to_same_host_are_spaced(self):
        """Two back-to-back acquisitions for one host are >= min_interval apart.

        Deliberately kept on the real clock: the sibling tests freeze it, so
        without this one nothing would check that the limiter actually sleeps.
        A *lower* bound on elapsed time is safe under load — a starved scheduler
        can only make this wait longer, never shorter — which is exactly what
        the load-sensitive assertions removed in #406 could not say.
        """
        limiter = _http._HostRateLimiter(min_interval=0.1)
        start = time.monotonic()
        limiter.wait("aopwiki.org")
        limiter.wait("aopwiki.org")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.1, f"second call should wait, elapsed={elapsed:.3f}s"

    def test_different_hosts_are_independent(self, monkeypatch):
        """A request to a different host is not delayed by another host.

        Asserted on the *slot the limiter reserves*, not on elapsed wall-clock.
        An upper bound on elapsed time is load-sensitive by construction: a
        starved scheduler makes a correct limiter look slow, so the test fails
        for a reason that has nothing to do with the property under test.
        """
        clock = _FrozenClock()
        monkeypatch.setattr(_http, "time", clock)
        limiter = _http._HostRateLimiter(min_interval=0.5)

        limiter.wait("aopwiki.org")
        limiter.wait("pubchem.ncbi.nlm.nih.gov")

        # Neither host blocked the other: both were granted immediately, so
        # neither call slept at all.
        assert clock.sleeps == [], f"distinct hosts blocked each other: {clock.sleeps}"

    def test_concurrent_calls_do_not_exceed_rate_cap(self, monkeypatch):
        """N concurrent acquisitions for one host reserve N spaced-out slots.

        The rate cap lives entirely in the slot reservation, which happens under
        the limiter's lock and is therefore scheduler-independent. The previous
        version measured wall-clock *after* ``wait`` returned, which is a
        different quantity: a thread descheduled between the grant and the
        timestamp bunches two grants together and fails a correct limiter. That
        is what made this flaky under ``-n auto`` (#406).

        Freezing the clock removes wall-clock from the assertion entirely and
        makes the check exact rather than a tolerance-padded lower bound, so it
        is also strictly stronger than what it replaces.
        """
        clock = _FrozenClock()
        monkeypatch.setattr(_http, "time", clock)
        interval = 0.05
        limiter = _http._HostRateLimiter(min_interval=interval)
        n = 6
        barrier = threading.Barrier(n)

        def worker() -> None:
            barrier.wait()  # release all threads at once
            limiter.wait("aopwiki.org")

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # The first caller is granted immediately and never sleeps; the other
        # n-1 are queued behind it at exactly one interval each. Order across
        # threads is arbitrary, the multiset is not.
        expected = [round(interval * k, 6) for k in range(1, n)]
        assert sorted(round(s, 6) for s in clock.sleeps) == expected
        # And the host's next free slot is n intervals out — the cap was never
        # exceeded by parallelism.
        assert limiter._next_allowed["aopwiki.org"] == pytest.approx(clock.now + n * interval)
