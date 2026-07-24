"""Config — multi-source configuration resolution.

Priority: CLI args > environment variables > config file > defaults.

Config file: ~/.microagent/config.yaml
Environment: MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL,
             MICROAGENT_SYSTEM_PROMPT, MICROAGENT_SKILLS_PATH
CLI args:    --base-url, --api-key, --model, --system-prompt
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .llm.client import LLMConfig


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved configuration for an Agent."""

    llm: LLMConfig
    system_prompt: str = "You are a helpful assistant."
    skills_path: str | None = None  # colon-separated list of skill directories
    toolset: str = "core,extended"  # comma-separated toolset layers

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

        return cls(
            llm=LLMConfig(base_url=base_url, api_key=api_key, model=model),
            system_prompt=system_prompt,
            skills_path=skills_path,
        )

    @staticmethod
    def _config_path() -> Path:
        return Path.home() / ".microagent" / "config.yaml"

    @staticmethod
    def _read_config_file() -> dict[str, str]:
        path = Config._config_path()
        if not path.exists():
            return {}

        try:
            import yaml

            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            return {}

        model_section = data.get("model", {})
        if not isinstance(model_section, dict):
            model_section = {}

        return {
            "base_url": model_section.get("base_url"),
            "api_key": model_section.get("api_key"),
            "model": model_section.get("model"),
            "system_prompt": data.get("system_prompt"),
            "skills_path": data.get("skills_path"),
        }
