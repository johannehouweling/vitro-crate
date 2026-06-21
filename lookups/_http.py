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

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = 10
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


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
    adapter = HTTPAdapter(max_retries=retry)
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
    try:
        resp = get_session().get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise TransientLookupError(f"request error for {url}: {exc}") from exc

    code = resp.status_code
    if code == 200:
        try:
            return resp.json()
        except ValueError as exc:
            raise TransientLookupError(f"invalid JSON body from {url}: {exc}") from exc
    if code in _TRANSIENT_STATUS:
        raise TransientLookupError(f"HTTP {code} from {url} after retries")
    # 404 and any other non-retryable client/redirect status → definitive.
    return NOT_FOUND
