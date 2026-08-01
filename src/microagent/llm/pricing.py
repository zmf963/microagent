"""Model pricing + context-window lookup with a local cache.

The cache is seeded from ``models.dev`` (the open models.dev directory at
https://models.dev/api.json, mirrored at
raw.githubusercontent.com/anomalyco/models.dev/dev/models.json) and shipped
inside the package as ``models_cache.json`` so cost tracking works offline.

Resolution order for a model id (e.g. ``"openai/gpt-4o"`` or ``"tx-d4f"``):
  1. Exact match in the cache (e.g. ``"openai/gpt-4o"``).
  2. Longest-prefix match in the cache (e.g. ``"deepseek-v4-pro-2026-01"``
     matches ``"deepseek/deepseek-v4-pro"`` after we also try the bare id).
  3. Local overrides in ``_LOCAL_OVERRIDES`` — for self-hosted / gateway
     aliases (tx-d4f, oc-d4f, tx-d4p) that aren't in models.dev because
     they're free local deployments.
  4. Conservative fallback ($0.50/1M both ways, 128K context) so Budget
     cost tracking never silently zeros out for an unknown model.

``refresh()`` re-downloads from models.dev and updates the on-disk cache +
the in-memory copy. Network failures fall back to the existing cache with
a warning — the agent keeps working.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_FILE = Path(__file__).resolve().parent / "models_cache.json"
_REMOTE_URL = "https://models.dev/api.json"
# Mirror reachable from networks where models.dev itself is blocked
# (e.g. CN). Same data, fetched from the source repo's default branch.
_REMOTE_MIRROR = (
    "https://raw.githubusercontent.com/anomalyco/models.dev/dev/models.json"
)

# Local overrides for models that models.dev doesn't track: self-hosted /
# gateway-aliased deployments that are free (a local 9router gateway
# serving DeepSeek-V4-Flash under compact aliases). Values are
# (input_per_1m, output_per_1m, context_length). Without these, the
# aliases would fall through to the $0.50/1M fallback and over-charge.
_LOCAL_OVERRIDES: dict[str, tuple[float, float, int]] = {
    "tx-d4f": (0.0, 0.0, 200_000),
    "oc-d4f": (0.0, 0.0, 200_000),
    "tx-d4p": (0.0, 0.0, 200_000),
}

_FALLBACK_PRICE = (0.50, 0.50)
_FALLBACK_CONTEXT = 128_000

# In-memory cache: model_id -> {input_per_1m, output_per_1m, context_length}
_cache: dict[str, dict[str, Any]] = {}
_cache_loaded = False


def _load_cache() -> None:
    """Populate _cache from the on-disk seed file (idempotent)."""
    global _cache_loaded
    if _cache_loaded:
        return
    try:
        data = json.loads(_CACHE_FILE.read_text())
        models = data.get("models", data) if isinstance(data, dict) else data
        if isinstance(models, dict):
            _cache.update(models)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load models cache %s: %r", _CACHE_FILE, e)
    _cache_loaded = True


def _normalize_id(model: str) -> str:
    """Best-effort normalization of a model id for cache lookup.

    models.dev uses ``provider/model`` ids (e.g. ``openai/gpt-4o``); OpenAI-
    style API responses use bare ids (e.g. ``gpt-4o``). We try both.
    """
    m = model.strip()
    if not m:
        return m
    # If the id already has a provider prefix, use it as-is.
    if "/" in m:
        return m
    # Bare id: try matching the tail against cached "provider/model" keys.
    return m


def _lookup(model: str) -> dict[str, Any] | None:
    """Find a cache entry for `model` (exact then suffix then prefix)."""
    if not model:
        return None
    m = model.strip().lower()
    # 1. Exact (case-insensitive) match on the id as given.
    for key in (model, m):
        if key in _cache:
            return _cache[key]
    # 2. The cache keys are "provider/model"; a bare "gpt-4o" should match
    #    "openai/gpt-4o". Try suffix match on the model tail.
    if "/" not in model:
        candidates = [k for k in _cache if k.lower().endswith("/" + m)]
        if len(candidates) == 1:
            return _cache[candidates[0]]
        # Multiple providers ship a model with the same tail (e.g. "gpt-4o"
        # is OpenAI-only in practice, but be safe): prefer the shortest key.
        if candidates:
            candidates.sort(key=len)
            return _cache[candidates[0]]
    # 3. Longest-prefix match for dated/suffixed variants
    #    (e.g. "openai/gpt-4o-2024-08-06" → "openai/gpt-4o").
    best: tuple[int, dict] | None = None
    for key, entry in _cache.items():
        kl = key.lower()
        if m.startswith(kl) or kl.endswith("/" + m.split("/")[-1]):
            if best is None or len(kl) > best[0]:
                best = (len(kl), entry)
    if best is not None:
        return best[1]
    return None


def get_pricing(model: str) -> tuple[float, float]:
    """Return (input_price_per_1M, output_price_per_1M) for `model`.

    Falls back to a conservative $0.50/1M for unknown models so Budget cost
    tracking never silently zeros out (which would let an expensive model
    burn through the budget unreported).
    """
    _load_cache()
    # Local overrides take precedence (gateway aliases aren't in models.dev).
    if model in _LOCAL_OVERRIDES:
        return _LOCAL_OVERRIDES[model][:2]
    entry = _lookup(model)
    if entry is not None:
        try:
            return (
                float(entry.get("input_per_1m", _FALLBACK_PRICE[0])),
                float(entry.get("output_per_1m", _FALLBACK_PRICE[1])),
            )
        except (TypeError, ValueError):
            pass
    return _FALLBACK_PRICE


def get_context_window(model: str) -> int:
    """Return the context window (tokens) for `model`, prefix-matched."""
    _load_cache()
    if model in _LOCAL_OVERRIDES:
        return _LOCAL_OVERRIDES[model][2]
    entry = _lookup(model)
    if entry is not None:
        ctx = entry.get("context_length")
        if isinstance(ctx, int) and ctx > 0:
            return ctx
    return _FALLBACK_CONTEXT


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from token counts and model pricing."""
    in_price, out_price = get_pricing(model)
    return (
        input_tokens / 1_000_000 * in_price
        + output_tokens / 1_000_000 * out_price
    )


def refresh(timeout: float = 20.0) -> int:
    """Re-download the models.dev catalog and refresh the cache.

    Tries the primary URL, then the GitHub mirror. On any network error,
    keeps the existing cache and logs a warning (the agent keeps working).

    Returns the number of models in the refreshed cache, or the current
    cache size if all fetches failed.
    """
    global _cache_loaded, _cache
    raw = None
    for url in (_REMOTE_URL, _REMOTE_MIRROR):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "microagent/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            logger.info("Refreshed models cache from %s", url)
            break
        except Exception as e:
            logger.warning("Fetch from %s failed: %r", url, e)
            continue
    if raw is None:
        logger.warning(
            "All models.dev fetches failed; keeping existing cache (%d models)",
            len(_cache),
        )
        return len(_cache)

    models = raw.get("data", raw) if isinstance(raw, dict) else raw
    new_cache: dict[str, dict[str, Any]] = {}
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        p = m.get("pricing", {}) or {}
        try:
            inp = float(p.get("prompt", 0)) * 1_000_000
            out = float(p.get("completion", 0)) * 1_000_000
        except (TypeError, ValueError):
            continue
        ctx = m.get("context_length")
        new_cache[mid] = {
            "name": m.get("name", mid),
            "input_per_1m": round(inp, 6),
            "output_per_1m": round(out, 6),
            "context_length": ctx if isinstance(ctx, int) and ctx > 0 else None,
        }

    # Persist (best-effort) so the refresh survives a restart.
    try:
        payload = {
            "_source": f"refreshed from {url}",
            "_note": "prices in USD per 1M tokens; context_length in tokens",
            "models": new_cache,
        }
        _CACHE_FILE.write_text(json.dumps(payload, indent=2))
    except OSError as e:
        logger.warning("Could not persist refreshed cache: %r", e)

    _cache = new_cache
    _cache_loaded = True
    return len(new_cache)


def known_models() -> list[str]:
    """Return the sorted list of model ids in the cache (for debugging)."""
    _load_cache()
    return sorted(_cache.keys())
