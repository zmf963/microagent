"""Currency display helpers.

Cost is tracked internally in USD — Budget limits, the models.dev pricing
cache, and ``Usage.cost_usd`` all stay in USD so accounting is stable and
the cache needs no conversion. Conversion to the display currency (CNY by
default) happens ONLY at the presentation boundary via these helpers.

The exchange rate is configurable via the ``MICROAGENT_CURRENCY_RATE``
environment variable (CNY per 1 USD); the default is a recent approximate
rate. Keeping it a constant (rather than fetching from a live FX API)
makes display deterministic and avoids a network dependency in a hot
display path — CN networks often can't reach public FX endpoints anyway.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Default USD→CNY rate (mid-2026 approximation). Override via the
# MICROAGENT_CURRENCY_RATE env var if you need a different rate.
_DEFAULT_USD_TO_CNY = 7.20


def get_usd_to_cny_rate() -> float:
    """Return the current USD→CNY conversion rate (CNY per 1 USD).

    Resolution: ``MICROAGENT_CURRENCY_RATE`` env var > default. A non-positive
    or unparseable value falls back to the default (rather than silently
    zeroing or crashing display).
    """
    env = os.environ.get("MICROAGENT_CURRENCY_RATE")
    if env:
        try:
            rate = float(env)
            if rate > 0:
                return rate
            logger.warning(
                "MICROAGENT_CURRENCY_RATE=%r is non-positive; using default %s",
                env, _DEFAULT_USD_TO_CNY,
            )
        except ValueError:
            logger.warning(
                "MICROAGENT_CURRENCY_RATE=%r is not a number; using default %s",
                env, _DEFAULT_USD_TO_CNY,
            )
    return _DEFAULT_USD_TO_CNY


def usd_to_cny(usd: float) -> float:
    """Convert a USD amount to CNY at the current rate."""
    return usd * get_usd_to_cny_rate()


def format_cost(usd: float) -> str:
    """Format a USD cost as a CNY display string, e.g. ``¥0.0144``.

    Use for cumulative session cost in the status line and /cost output.
    Four decimal places matches the previous USD precision (¥0.0144 ≈
    $0.002 at 7.2 rate).
    """
    return f"¥{usd_to_cny(usd):.4f}"


def format_price_per_1m(usd_per_1m: float) -> str:
    """Format a per-1M-token price in CNY, e.g. ``¥18.00/1M``."""
    return f"¥{usd_to_cny(usd_per_1m):.4f}/1M"
