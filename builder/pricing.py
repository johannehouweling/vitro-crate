"""Token pricing from LiteLLM's model_prices_and_context_window.json.

Fetches the JSON from GitHub at import time (once) and provides
``get_model_cost()`` for computing token costs in the dashboard.

Key format in the JSON is ``"{provider}/{model_name}"`` lower-case,
e.g.  ``"openai/gpt-4o"``, ``"deepseek/deepseek-chat"``,
``"azure/deepseek-chat"``.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
_CACHE_DIR = Path.home() / ".cache" / "vitro-crate"
_CACHE_PATH = _CACHE_DIR / "model_prices.json"
_CACHE_TTL = 86400  # 24 hours

# Populated on first call to _ensure_loaded()
_PRICES: dict[str, dict[str, Any]] | None = None


def _fetch_prices() -> dict[str, dict[str, Any]] | None:
    """Download LiteLLM pricing JSON from GitHub."""
    import urllib.request

    req = urllib.request.Request(_PRICES_URL, headers={"User-Agent": "vitro-crate/1.0"})
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        logger.warning("Failed to fetch pricing data from LiteLLM", exc_info=True)
        return None


def _load_cached() -> dict[str, dict[str, Any]] | None:
    """Load pricing from cache if fresh."""
    if not _CACHE_PATH.exists():
        return None
    try:
        import time

        mtime = _CACHE_PATH.stat().st_mtime
        if time.time() - mtime > _CACHE_TTL:
            return None
        with open(_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(data: dict[str, dict[str, Any]]) -> None:
    """Write pricing data to cache."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _ensure_loaded() -> None:
    """Ensure _PRICES is populated (cache → fetch → empty fallback)."""
    global _PRICES
    if _PRICES is not None:
        return

    # Try cache first
    data = _load_cached()
    if data is not None:
        _PRICES = data
        return

    # Fetch from GitHub
    data = _fetch_prices()
    if data is not None:
        _PRICES = data
        _save_cache(data)
        return

    # Fallback: empty dict — cost will show "—"
    _PRICES = {}
    logger.warning("No pricing data available — costs will not be shown.")


def _weighted_cost(entry: dict[str, Any]) -> float:
    """Compute a conservative cost score using a 10:1 input:output ratio.

    Returns 0 if no rates are available (won't be selected).
    """
    in_rate = entry.get("input_cost_per_token")
    out_rate = entry.get("output_cost_per_token")
    if in_rate is None and out_rate is None:
        return 0.0
    in_rate = in_rate or 0.0
    out_rate = out_rate or 0.0
    return 10.0 * in_rate + 1.0 * out_rate


def get_model_cost(
    model_name: str,
    provider: str | None = None,
) -> dict[str, Any] | None:
    """Look up pricing info for *model_name* (case-insensitive).

    When no *provider* is specified (or no exact match by provider), and
    multiple providers carry the same model name, the **most expensive**
    option is always returned — this gives a conservative cost ceiling.
    Cost is weighted 10:1 input:output since you typically put in far more
    tokens than you get out.

    Args:
        model_name: The model name as reported by the LLM (e.g. ``"gpt-4o"``,
            ``"deepseek-chat"``).
        provider: Optional provider prefix (e.g. ``"deepseek"``, ``"azure"``,
            ``"openai"``).  When provided, tries ``{provider}/{model_name}``
            first.

    Returns:
        A dict with pricing keys (``input_cost_per_token``,
        ``output_cost_per_token``, ``max_input_tokens``, etc.) or *None* if
        no match found.
    """
    _ensure_loaded()
    if not _PRICES:
        return None

    name_lower = model_name.strip().lower()

    # 1) Try exact key match (already lower-case in the JSON)
    if provider:
        key = f"{provider.strip().lower()}/{name_lower}"
        entry = _PRICES.get(key)
        if entry is not None:
            return entry

    # 2) Try ``*/{name_lower}`` — collect all matches, pick most expensive
    candidates: list[dict[str, Any]] = []
    for key, entry in _PRICES.items():
        if key.endswith(f"/{name_lower}"):
            candidates.append(entry)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return max(candidates, key=_weighted_cost)

    # 3) Try ``{name_lower}`` bare
    entry = _PRICES.get(name_lower)
    if entry is not None:
        return entry

    # 4) Try matching just the model name at end of key (e.g. key="openai/gpt-4o-2024-08-06"
    #    and model_name="gpt-4o") — again pick most expensive
    prefix_candidates: list[dict[str, Any]] = []
    for key, entry in _PRICES.items():
        if "/" in key and key.split("/")[-1].startswith(name_lower):
            prefix_candidates.append(entry)

    if len(prefix_candidates) == 1:
        return prefix_candidates[0]
    if len(prefix_candidates) > 1:
        return max(prefix_candidates, key=_weighted_cost)

    return None


def compute_cost(
    input_tokens: int,
    output_tokens: int,
    model_name: str,
    provider: str | None = None,
) -> dict[str, float | None]:
    """Compute the cost of a token usage given a model and optional provider.

    When no *provider* is given, ``get_model_cost`` automatically picks the
    most expensive option among multiple candidates (conservative 10:1
    input:output weighting).  See :func:`get_model_cost` for details.

    Args:
        input_tokens: Number of input (prompt) tokens.
        output_tokens: Number of output (completion) tokens.
        model_name: The model name (e.g. ``\"gpt-4o\"``, ``\"deepseek-chat\"``).
        provider: Optional provider prefix. When *None*, the most expensive
            provider for this model is picked automatically.

    Returns a dict with ``input_cost``, ``output_cost``, ``total_cost``
    (each a float or *None* if pricing is unavailable).
    """
    pricing = get_model_cost(model_name, provider)
    if pricing is None:
        return {"input_cost": None, "output_cost": None, "total_cost": None}

    in_rate = pricing.get("input_cost_per_token")
    out_rate = pricing.get("output_cost_per_token")
    input_cost = (in_rate * input_tokens) if in_rate is not None else None
    output_cost = (out_rate * output_tokens) if out_rate is not None else None
    total_cost: float | None = None
    if input_cost is not None and output_cost is not None:
        total_cost = round(input_cost + output_cost, 8)
    elif input_cost is not None:
        total_cost = round(input_cost, 8)
    elif output_cost is not None:
        total_cost = round(output_cost, 8)

    return {
        "input_cost": round(input_cost, 8) if input_cost is not None else None,
        "output_cost": round(output_cost, 8) if output_cost is not None else None,
        "total_cost": total_cost,
    }


def format_cost(cost: float | None) -> str:
    """Format a cost value for display."""
    if cost is None:
        return "—"
    if cost < 0.01:
        return f"${cost:.6f}"
    return f"${cost:.4f}"


def list_providers() -> list[str]:
    """Return an alphabetically sorted list of unique provider prefixes
    from the LiteLLM pricing JSON (e.g. ``openai``, ``azure``, ``deepseek``).

    Filters out non-vendor keys like image-dimension prefixes (e.g.
    ``1024-x-1024``). Returns an empty list if pricing data is not available.
    """
    _ensure_loaded()
    if not _PRICES:
        return []

    # Patterns that are definitely not vendor prefixes
    _IGNORED_PREFIX_RE = re.compile(r"^\d+[x-]|^ft:|^openai-large-|^together-")

    providers: set[str] = set()
    for key in _PRICES:
        if "/" not in key:
            continue
        prefix = key.split("/")[0]
        if _IGNORED_PREFIX_RE.match(prefix):
            continue
        providers.add(prefix)
    return sorted(providers)


__all__ = [
    "get_model_cost",
    "compute_cost",
    "format_cost",
    "list_providers",
]
