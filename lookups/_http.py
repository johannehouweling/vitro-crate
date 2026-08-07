"""Shared resilient HTTP helper for the lookup clients.

All lookup clients hit third-party APIs (PubChem, Cellosaurus, AOP-Wiki,
OLS/BAO, ORCID, ROR, Crossref). A single dropped packet, read timeout, or a
429/503 throttle used to turn into a hard "not found" for the rest of the run
— and because the clients are ``lru_cache``d, that failure was negatively
cached too.

This helper centralizes a :class:`requests.Session` with automatic retry +
exponential backoff (honoring ``Retry-After``) and a clear three-way outcome:

* **success** — HTTP 200 with a valid JSON body → the parsed object.
* **definitive not-found** — HTTP 404 (or other non-retryable 4xx) →
  :data:`NOT_FOUND` (a sentinel). Callers map this to their empty result.
* **transient failure** — timeout, connection error, or 429/5xx that persists
  after retries, or a malformed 200 body → :class:`TransientLookupError`.

Because a transient failure *raises*, ``lru_cache`` never stores it, so a
later call in the same session re-hits the network instead of returning a
stale failure.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

# Minimum spacing (seconds) between successive requests to the *same* host.
# This is the politeness throttle that replaces the per-client
# ``time.sleep(0.1)`` calls (#62). Centralising it here means the cap is
# honoured even when independent requests are issued concurrently (e.g. AOP-Wiki
# event details fetched via a ThreadPoolExecutor). Different hosts are throttled
# independently, so parallel lookups across services are not serialised.
_HOST_MIN_INTERVAL = 0.1


class TransientLookupError(Exception):
    """A lookup failed transiently (timeout / connection / 429 / 5xx).

    Distinct from a definitive not-found so callers can keep the user's value
    and retry later instead of treating it as "not found".
    """


class _NotFound:
    """Sentinel type for a definitive not-found response."""

    _instance: _NotFound | None = None

    def __new__(cls) -> _NotFound:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "NOT_FOUND"

    def __bool__(self) -> bool:
        return False


NOT_FOUND = _NotFound()


class _HostRateLimiter:
    """Thread-safe per-host throttle enforcing a minimum spacing between requests.

    ``wait(host)`` blocks until at least ``min_interval`` seconds have elapsed
    since the previous grant *for that host*, then records the grant time. Hosts
    are tracked independently, so a slow request to one API never throttles a
    request to a different API. A single lock guards the per-host timestamps and
    serialises the wait, so concurrent callers for the same host are spaced out
    deterministically rather than all firing at once.
    """

    def __init__(self, min_interval: float = _HOST_MIN_INTERVAL) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait(self, host: str) -> None:
        """Block until this host is allowed to fire its next request."""
        interval = self._min_interval
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            earliest = self._next_allowed.get(host, 0.0)
            start = max(now, earliest)
            # Reserve the slot *before* releasing the lock so concurrent callers
            # for the same host queue up behind us instead of colliding.
            self._next_allowed[host] = start + interval
            sleep_for = start - now
        if sleep_for > 0:
            time.sleep(sleep_for)

    def reset(self) -> None:
        """Forget all recorded per-host timestamps (used by tests)."""
        with self._lock:
            self._next_allowed.clear()


_host_throttle = _HostRateLimiter()


def _host_of(url: str) -> str:
    """Return the network location (host[:port]) of a URL, or '' if none."""
    return urlsplit(url).netloc


def throttle_for_url(url: str) -> None:
    """Apply the shared per-host politeness throttle for ``url``.

    Exposed so callers that issue requests through their own code path (e.g.
    concurrent AOP-Wiki event-detail fetches) get the same per-host spacing as
    :func:`http_get_json`. The interval is read live from ``_HOST_MIN_INTERVAL``
    so tests can tune it via monkeypatch.
    """
    _host_throttle._min_interval = _HOST_MIN_INTERVAL
    _host_throttle.wait(_host_of(url))


def reset_host_throttle() -> None:
    """Reset the shared per-host throttle state (used by tests)."""
    _host_throttle.reset()


# Connection-pool size. Matches the parallel tool execution above it, so a batch
# of lookups reuses connections instead of thrashing the pool.
_POOL_SIZE = 24

# --- per-host circuit breaker -------------------------------------------------
# A host that has failed transiently several times in a row is down, not busy.
# Without this, every remaining lookup pays the full retry budget against it:
# one observed outage cost 84 seconds across 22 compounds (3 attempts x 10s
# timeout each), all to learn what the first call already established. After
# _BREAKER_THRESHOLD consecutive failures the host is skipped for
# _BREAKER_COOLDOWN seconds — callers get the same TransientLookupError they
# would have got anyway, just immediately, and one probe after the cooldown
# re-opens it if the service came back.
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN = 60.0
_breaker_state: dict[str, tuple[int, float]] = {}
_breaker_lock = threading.Lock()


def _breaker_open_for(host: str) -> float:
    """Seconds remaining on *host*'s cooldown, or 0.0 when it may be called."""
    with _breaker_lock:
        _failures, open_until = _breaker_state.get(host, (0, 0.0))
    return max(0.0, open_until - time.monotonic())


def _breaker_record(host: str, *, ok: bool) -> None:
    """Note a success (closes the breaker) or a transient failure (may open it)."""
    with _breaker_lock:
        if ok:
            _breaker_state.pop(host, None)
            return
        failures, _open_until = _breaker_state.get(host, (0, 0.0))
        failures += 1
        open_until = (
            time.monotonic() + _BREAKER_COOLDOWN if failures >= _BREAKER_THRESHOLD else 0.0
        )
        _breaker_state[host] = (failures, open_until)
        if open_until:
            logger.warning(
                "%s failed %d times running — skipping it for %.0fs "
                "(lookups against it will report unavailable immediately)",
                host,
                failures,
                _BREAKER_COOLDOWN,
            )


def reset_circuit_breaker() -> None:
    """Forget all breaker state (used by tests)."""
    with _breaker_lock:
        _breaker_state.clear()


_session: requests.Session | None = None


def _build_session() -> requests.Session:
    """Build a Session whose adapters retry transient failures with backoff."""
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=tuple(_TRANSIENT_STATUS),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    # Sized for the tool layer's concurrency: a model that resolves 22 compounds
    # emits them as one parallel batch (16 at a time observed), and urllib3's
    # default pool of 10 then spends the run discarding and re-opening
    # connections ("Connection pool is full") against the very host that is
    # already struggling.
    adapter = HTTPAdapter(max_retries=retry, pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_session() -> requests.Session:
    """Return the shared, lazily-built, retry-enabled Session."""
    global _session
    if _session is None:
        _session = _build_session()
    return _session


def http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """GET ``url`` and return parsed JSON, ``NOT_FOUND``, or raise transiently.

    Args:
        url: The URL to GET.
        params: Optional query parameters.
        headers: Optional request headers.
        timeout: Per-request timeout in seconds.

    Returns:
        The parsed JSON object on HTTP 200, or :data:`NOT_FOUND` on a
        definitive 4xx (404 and other non-retryable client errors).

    Raises:
        TransientLookupError: on timeout, connection error, a 429/5xx that
            survived retries, or a 200 with a malformed JSON body.
    """
    # Politeness: space requests to the same host (replaces the per-client
    # ``time.sleep(0.1)`` calls). Honoured even under concurrent callers (#62).
    host = _host_of(url)
    cooling = _breaker_open_for(host)
    if cooling:
        # Same error the call would have raised, without the wait.
        raise TransientLookupError(
            f"{host} is unavailable (failed repeatedly; retrying in {cooling:.0f}s)"
        )
    throttle_for_url(url)
    try:
        resp = get_session().get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        _breaker_record(host, ok=False)
        raise TransientLookupError(f"request error for {url}: {exc}") from exc

    code = resp.status_code
    if code == 200:
        try:
            payload = resp.json()
        except ValueError as exc:
            _breaker_record(host, ok=False)
            raise TransientLookupError(f"invalid JSON body from {url}: {exc}") from exc
        _breaker_record(host, ok=True)
        return payload
    if code in _TRANSIENT_STATUS:
        _breaker_record(host, ok=False)
        raise TransientLookupError(f"HTTP {code} from {url} after retries")
    # 404 and any other non-retryable client/redirect status → definitive. The
    # host ANSWERED, so it is healthy: a run of "no such chemical" must never
    # trip the breaker.
    _breaker_record(host, ok=True)
    return NOT_FOUND
