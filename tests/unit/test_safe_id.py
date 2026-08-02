"""Tests for microagent.tools.safe_id — path-traversal-hardening helpers."""

import pytest

from microagent.tools.safe_id import is_safe_name, safe_filename_from_id


class TestIsSafeName:
    @pytest.mark.parametrize("name", [
        "my-skill", "my_skill", "Skill1", "a.b.c", "tdd", "x", "0abc",
        "a-b_c.d", "Z9", "A" * 255,
    ])
    def test_accepts_valid(self, name):
        assert is_safe_name(name) is True

    @pytest.mark.parametrize("name", [
        "", ".", "..", "../etc/passwd", "/etc/passwd", "a/b", "a\\b",
        "a b", "-x", "_x", ".hidden", "a..b", "..hidden", "name ", " na",
        "x" * 256, "a.b/c", "café", "🎉", "a;b", "a$b", "a`b", "a|b",
        "a&b", "a>b", "a<b", "a*b", "a?b", "a\"b", "a'b",
    ])
    def test_rejects_invalid(self, name):
        assert is_safe_name(name) is False

    def test_rejects_empty_string(self):
        assert is_safe_name("") is False

    def test_rejects_whitespace_only(self):
        assert is_safe_name("   ") is False


class TestSafeFilenameFromId:
    def test_returns_32_hex_chars(self):
        result = safe_filename_from_id("call_abc123")
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self):
        assert safe_filename_from_id("x") == safe_filename_from_id("x")

    def test_different_inputs_differ(self):
        assert safe_filename_from_id("a") != safe_filename_from_id("b")

    def test_neutralizes_traversal(self):
        # A traversal id must NOT appear in the resulting filename
        result = safe_filename_from_id("../../etc/cron.d/evil")
        assert "/" not in result
        assert ".." not in result
        assert "evil" not in result

    def test_handles_unicode(self):
        result = safe_filename_from_id("技能名🎉")
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_handles_empty(self):
        result = safe_filename_from_id("")
        assert len(result) == 32
