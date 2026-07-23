"""Tests for Config — multi-source config resolution."""

import os
import pytest
from pathlib import Path
from microagent.config import Config, LLMConfig


class TestConfig:
    def test_from_file(self, tmp_path, monkeypatch):
        """Config reads from ~/.microagent/config.yaml."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
model:
  base_url: "http://my-server/v1"
  api_key: "sk-test"
  model: "my-model"
system_prompt: "Custom system prompt."
""")
        monkeypatch.setattr(Config, "_config_path", lambda: config_file)
        cfg = Config.from_file()
        assert cfg.llm.base_url == "http://my-server/v1"
        assert cfg.llm.api_key == "sk-test"
        assert cfg.llm.model == "my-model"
        assert cfg.system_prompt == "Custom system prompt."

    def test_from_file_defaults(self, tmp_path, monkeypatch):
        """Missing config file → safe defaults."""
        monkeypatch.setattr(Config, "_config_path", lambda: tmp_path / "nonexistent.yaml")
        cfg = Config.from_file()
        assert cfg.llm.base_url == "https://api.openai.com/v1"
        assert cfg.llm.model == "gpt-4o"
        assert cfg.system_prompt == "You are a helpful assistant."

    def test_env_override(self, tmp_path, monkeypatch):
        """Environment variables override config file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
model:
  base_url: "http://file-server/v1"
  model: "file-model"
""")
        monkeypatch.setattr(Config, "_config_path", lambda: config_file)
        monkeypatch.setenv("MICROAGENT_BASE_URL", "http://env-server/v1")
        monkeypatch.setenv("MICROAGENT_MODEL", "env-model")

        cfg = Config.from_file()
        # Env overrides file
        assert cfg.llm.base_url == "http://env-server/v1"
        assert cfg.llm.model == "env-model"

    def test_cli_override(self, tmp_path, monkeypatch):
        """CLI args override everything."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
model:
  base_url: "http://file-server/v1"
  model: "file-model"
""")
        monkeypatch.setattr(Config, "_config_path", lambda: config_file)
        monkeypatch.setenv("MICROAGENT_BASE_URL", "http://env-server/v1")

        cfg = Config.from_file(cli_base_url="http://cli-server/v1")
        # CLI > env > file
        assert cfg.llm.base_url == "http://cli-server/v1"
        assert cfg.llm.model == "file-model"  # no CLI or env override for model

    def test_cli_args_none_ignored(self, tmp_path, monkeypatch):
        """CLI args that are None don't override."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
model:
  model: "file-model"
""")
        monkeypatch.setattr(Config, "_config_path", lambda: config_file)

        cfg = Config.from_file(cli_model=None)
        assert cfg.llm.model == "file-model"
