"""Tests for CLI slash commands and token usage tracking.

Tests the command registry pattern: /model, /history, /skill, /clear, /cost.
Token usage is tracked locally in the CLI and displayed via /cost.
"""

import io
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from microagent.core.types import Message, TurnComplete, Usage
from microagent.surface.cli import _UsageTracker


class TestUsageTracker:
    def test_initial_state(self):
        """UsageTracker starts with zero usage."""
        tracker = _UsageTracker()
        assert tracker.total_input == 0
        assert tracker.total_output == 0
        assert tracker.total_cost == 0.0
        assert tracker.turns == 0

    def test_record_usage(self):
        """Recording usage accumulates correctly."""
        tracker = _UsageTracker()
        tracker.record(Usage(input_tokens=100, output_tokens=50, cost_usd=0.01))
        tracker.record(Usage(input_tokens=200, output_tokens=75, cost_usd=0.02))
        assert tracker.total_input == 300
        assert tracker.total_output == 125
        assert tracker.total_cost == pytest.approx(0.03)
        assert tracker.turns == 2

    def test_reset(self):
        """Reset clears all accumulated usage."""
        tracker = _UsageTracker()
        tracker.record(Usage(input_tokens=100, output_tokens=50, cost_usd=0.01))
        tracker.reset()
        assert tracker.total_input == 0
        assert tracker.total_output == 0
        assert tracker.total_cost == 0.0
        assert tracker.turns == 0

    def test_summary_string(self):
        """Summary string contains key metrics.

        Cost is displayed in CNY (¥) — the internal USD value ($0.05) is
        converted at display time via currency.format_cost. At the default
        7.2 rate, $0.05 → ¥0.3600."""
        tracker = _UsageTracker()
        tracker.record(Usage(input_tokens=1000, output_tokens=500, cost_usd=0.05))
        summary = tracker.summary()
        assert "1000" in summary
        assert "500" in summary
        # Displays in CNY with ¥ symbol, not raw USD
        assert "¥" in summary
        assert "0.3600" in summary  # $0.05 × 7.2


class TestSlashCommands:
    def test_command_registry_exists(self):
        """CLI has a command registry dict."""
        from microagent.surface.cli import _COMMANDS

        assert isinstance(_COMMANDS, dict)
        assert "new" in _COMMANDS
        assert "list" in _COMMANDS
        assert "resume" in _COMMANDS
        assert "compact" in _COMMANDS
        assert "help" in _COMMANDS

    def test_new_commands_registered(self):
        """New v0.2 commands are registered."""
        from microagent.surface.cli import _COMMANDS

        assert "model" in _COMMANDS
        assert "history" in _COMMANDS
        assert "skill" in _COMMANDS
        assert "clear" in _COMMANDS
        assert "cost" in _COMMANDS

    def test_help_lists_all_commands(self):
        """/help output includes all commands."""
        from microagent.surface.cli import _COMMANDS

        # All commands should have descriptions
        for cmd_name in ["new", "list", "resume", "compact", "model", "history", "skill", "clear", "cost", "help"]:
            assert cmd_name in _COMMANDS
            handler, description = _COMMANDS[cmd_name]
            assert isinstance(description, str)
            assert len(description) > 0
