"""Config — multi-source configuration resolution.

Priority: CLI args > environment variables > config file > defaults.

Config file: ~/.microagent/config.yaml
Environment: MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL,
             MICROAGENT_SYSTEM_PROMPT, MICROAGENT_SKILLS_PATH
CLI args:    --base-url, --api-key, --model, --system-prompt
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

from .llm.client import LLMConfig
from .llm.templates import DEFAULT_TEMPLATE


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved configuration for an Agent."""

    llm: LLMConfig
    system_prompt: str = DEFAULT_TEMPLATE
    skills_path: str | None = None  # colon-separated list of skill directories

    @classmethod
    def from_file(
        cls,
        *,
        cli_base_url: str | None = None,
        cli_api_key: str | None = None,
        cli_model: str | None = None,
        cli_system_prompt: str | None = None,
        cli_skills_path: str | None = None,
    ) -> Config:
        """Load config from file, env, and CLI args (priority order)."""
        # 1. Load from config file
        file_data = cls._read_config_file()

        # 2. Resolve each field: CLI > env > file > default
        base_url = (
            cli_base_url
            or os.environ.get("MICROAGENT_BASE_URL")
            or file_data.get("base_url")
            or "https://api.openai.com/v1"
        )
        api_key = (
            cli_api_key or os.environ.get("MICROAGENT_API_KEY") or file_data.get("api_key") or ""
        )
        model = (
            cli_model or os.environ.get("MICROAGENT_MODEL") or file_data.get("model") or "gpt-4o"
        )
        system_prompt = (
            cli_system_prompt
            or os.environ.get("MICROAGENT_SYSTEM_PROMPT")
            or file_data.get("system_prompt")
            or "You are a helpful assistant."
        )
        skills_path = (
            cli_skills_path
            or os.environ.get("MICROAGENT_SKILLS_PATH")
            or file_data.get("skills_path")
        )
        # auxiliary/reasoning/service_tier/retry_policy were previously
        # NOT configurable at all — only a hand-built LLMConfig could set
        # them. Resolve env > file for each.
        auxiliary_model = (
            os.environ.get("MICROAGENT_AUXILIARY_MODEL")
            or file_data.get("auxiliary_model")
        )
        reasoning_effort = (
            os.environ.get("MICROAGENT_REASONING_EFFORT")
            or file_data.get("reasoning_effort")
        )
        service_tier = (
            os.environ.get("MICROAGENT_SERVICE_TIER")
            or file_data.get("service_tier")
        )
        retry_policy = (
            os.environ.get("MICROAGENT_RETRY_POLICY")
            or file_data.get("retry_policy")
        )

        return cls(
            llm=LLMConfig(
                base_url=base_url,
                api_key=api_key,
                model=model,
                auxiliary_model=auxiliary_model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                retry_policy=retry_policy if retry_policy else "normal",
            ),
            system_prompt=system_prompt,
            skills_path=skills_path,
        )

    @staticmethod
    def _config_path() -> Path:
        return Path.home() / ".microagent" / "config.yaml"

    @staticmethod
    def _read_config_file() -> dict[str, str | None]:
        path = Config._config_path()
        if not path.exists():
            return {}

        try:
            import yaml

            data = yaml.safe_load(path.read_text()) or {}
        except Exception as e:
            # A malformed/permission-denied config file silently fell back
            # to defaults — the user's API key/model/base_url vanished with
            # no warning, and the agent connected to the wrong endpoint.
            _logger.warning("Failed to read config file %s: %r", path, e)
            return {}

        if not isinstance(data, dict):
            # Syntactically valid YAML whose top level is a scalar or list
            # ("just a string", "- item") passed safe_load but has no
            # .get() — without this guard the file crashed startup with
            # AttributeError instead of falling back to defaults.
            _logger.warning(
                "Config file %s has non-mapping top level (%s) — ignoring",
                path, type(data).__name__,
            )
            return {}

        model_section = data.get("model", {})
        if not isinstance(model_section, dict):
            model_section = {}

        # Flat-layout trap: a user writing base_url/api_key/model at the
        # top level (the natural flat form) previously got the OpenAI
        # default endpoint + empty key + gpt-4o with ZERO warning — a
        # typo'd section name silently rerouted traffic. Warn loudly.
        if any(k in data for k in ("base_url", "api_key", "model")):
            _logger.warning(
                "Config file %s uses a flat layout (top-level base_url/api_key/"
                "model) — these keys must live under a 'model:' section. "
                "Flat values are IGNORED; check your config.",
                path,
            )

        return {
            "base_url": model_section.get("base_url"),
            "api_key": model_section.get("api_key"),
            "model": model_section.get("model"),
            "auxiliary_model": model_section.get("auxiliary_model"),
            "reasoning_effort": model_section.get("reasoning_effort"),
            "service_tier": model_section.get("service_tier"),
            "retry_policy": model_section.get("retry_policy"),
            "system_prompt": data.get("system_prompt"),
            "skills_path": data.get("skills_path"),
        }
