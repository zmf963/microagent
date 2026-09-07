"""Integration-test endpoint resolution (v1.2.0 matrix normalization).

Priority: MICROAGENT_TEST_* env vars > the user's ~/.microagent/config.yaml
> skip. The config fallback makes the matrix runnable on any machine with
a configured endpoint (`make integration`) — no env plumbing required —
while env vars still win for CI/one-off targets (different model without
touching the daily-driver config).
"""

from __future__ import annotations

import os


def _from_user_config() -> dict[str, str]:
    try:
        import yaml
    except ImportError:
        return {}
    path = os.path.expanduser("~/.microagent/config.yaml")
    if not os.path.exists(path):
        return {}
    try:
        data = yaml.safe_load(open(path)) or {}
    except Exception:
        return {}
    model = data.get("model") or {}
    if not isinstance(model, dict):
        return {}
    return {
        "MICROAGENT_TEST_BASE_URL": model.get("base_url") or "",
        "MICROAGENT_TEST_API_KEY": model.get("api_key") or "",
        "MICROAGENT_TEST_MODEL": model.get("model") or "",
    }


_FALLBACK = _from_user_config()


def resolve(key: str) -> str:
    """Env var first; the configured endpoint as fallback."""
    return os.environ.get(key) or _FALLBACK.get(key) or ""


def integration_ready() -> bool:
    return all(
        resolve(k)
        for k in (
            "MICROAGENT_TEST_BASE_URL",
            "MICROAGENT_TEST_API_KEY",
            "MICROAGENT_TEST_MODEL",
        )
    )
