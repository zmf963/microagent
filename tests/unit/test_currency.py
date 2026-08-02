"""Tests for the currency display helpers.

Internal cost stays in USD; conversion to CNY happens only at display via
these helpers. The rate is configurable via MICROAGENT_CURRENCY_RATE.
"""

import pytest

from microagent import currency


class TestRateResolution:
    def test_default_rate(self, monkeypatch):
        monkeypatch.delenv("MICROAGENT_CURRENCY_RATE", raising=False)
        assert currency.get_usd_to_cny_rate() == currency._DEFAULT_USD_TO_CNY

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MICROAGENT_CURRENCY_RATE", "7.35")
        assert currency.get_usd_to_cny_rate() == 7.35

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("MICROAGENT_CURRENCY_RATE", "not-a-number")
        assert currency.get_usd_to_cny_rate() == currency._DEFAULT_USD_TO_CNY

    def test_non_positive_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("MICROAGENT_CURRENCY_RATE", "0")
        assert currency.get_usd_to_cny_rate() == currency._DEFAULT_USD_TO_CNY
        monkeypatch.setenv("MICROAGENT_CURRENCY_RATE", "-1")
        assert currency.get_usd_to_cny_rate() == currency._DEFAULT_USD_TO_CNY


class TestConversion:
    def test_usd_to_cny_default_rate(self, monkeypatch):
        monkeypatch.delenv("MICROAGENT_CURRENCY_RATE", raising=False)
        # $1 at default 7.2 = ¥7.2
        assert currency.usd_to_cny(1.0) == pytest.approx(7.2)

    def test_usd_to_cny_custom_rate(self, monkeypatch):
        monkeypatch.setenv("MICROAGENT_CURRENCY_RATE", "7.0")
        assert currency.usd_to_cny(2.0) == pytest.approx(14.0)

    def test_zero_usd_is_zero_cny(self):
        assert currency.usd_to_cny(0.0) == 0.0


class TestFormatting:
    def test_format_cost_uses_yen_symbol(self, monkeypatch):
        monkeypatch.delenv("MICROAGENT_CURRENCY_RATE", raising=False)
        s = currency.format_cost(1.0)
        assert s.startswith("¥")
        # Default rate 7.2 → ¥7.2000
        assert "7.2000" in s

    def test_format_cost_four_decimals(self, monkeypatch):
        monkeypatch.setenv("MICROAGENT_CURRENCY_RATE", "7.2")
        # $0.002 → ¥0.0144
        assert currency.format_cost(0.002) == "¥0.0144"

    def test_format_price_per_1m(self, monkeypatch):
        monkeypatch.setenv("MICROAGENT_CURRENCY_RATE", "7.2")
        # $2.50/1M → ¥18.0000/1M
        assert currency.format_price_per_1m(2.50) == "¥18.0000/1M"

    def test_format_cost_respects_env_override(self, monkeypatch):
        monkeypatch.setenv("MICROAGENT_CURRENCY_RATE", "7.0")
        # $1 at 7.0 = ¥7.0000
        assert currency.format_cost(1.0) == "¥7.0000"


class TestInternalAccountingUnchanged:
    """Guard: the USD field on Usage/Budget must NOT be converted — only
    the display string changes. This keeps Budget comparisons stable and
    the models.dev cache in its native currency."""

    def test_usage_cost_usd_field_still_usd(self):
        from microagent.core.types import Usage
        u = Usage(input_tokens=1000, output_tokens=500, cost_usd=0.012)
        # The raw field is USD, unchanged
        assert u.cost_usd == 0.012

    def test_budget_max_cost_usd_still_usd(self):
        from microagent.session.budget import Budget
        b = Budget.root(max_cost_usd=5.0)
        assert b.max_cost_usd == 5.0  # not converted
