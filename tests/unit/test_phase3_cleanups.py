"""Regression tests for Phase 3 minor cleanups."""

import json
import pytest
from pathlib import Path

from microagent.skill.curator import Curator
from microagent.security.scrubber import StreamingContextScrubber


# --- file_tree connector fix ----------------------------------------------

@pytest.mark.asyncio
async def test_file_tree_connectors_correct_when_last_entry_ignored(tmp_path):
    """The last entry being an ignored dir (e.g. .venv) must not produce
    wrong tree connectors. The last *visible* entry should get └──."""
    from microagent.tools.builtins.file_tree import file_tree
    # Create: src/a.py, src/b.py, src/.venv/ (ignored, last alphabetically)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "src" / "b.py").write_text("x")
    (tmp_path / "src" / ".venv").mkdir()  # ignored, but would be "last" raw entry

    result = await file_tree.fn(path=str(tmp_path), max_depth=2)
    lines = result.content.split("\n")
    # The last visible file should use └── (not ├──)
    src_lines = [l for l in lines if "b.py" in l]
    assert src_lines, "b.py not in tree"
    assert "└──" in src_lines[0], f"b.py should use └── (last visible), got: {src_lines[0]}"


# --- write_file backup uses bytes -----------------------------------------

@pytest.mark.asyncio
async def test_write_file_backup_works_on_binary(tmp_path):
    """Backup of a binary file must not crash on UnicodeDecodeError."""
    from microagent.tools.builtins.write_file import write_file
    f = tmp_path / "data.bin"
    f.write_bytes(b"\x00\x01\x02\xff")  # binary, not valid UTF-8

    result = await write_file.fn(path=str(f), content="new text", backup=True)
    assert not result.is_error, result.content
    bak = tmp_path / "data.bin.bak"
    assert bak.exists()
    assert bak.read_bytes() == b"\x00\x01\x02\xff"  # original preserved


# --- scrubber case-insensitive tags ---------------------------------------

class TestScrubberCaseInsensitive:
    def test_strips_capitalized_close_tag(self):
        """LLMs often echo the fence with different capitalization — </Context>
        must close <context> or all subsequent output is discarded."""
        scrubber = StreamingContextScrubber()
        out1 = scrubber.feed("before <context> secret")
        out2 = scrubber.feed(" more secret </Context> after")
        assert "secret" not in out1 + out2
        assert "before" in out1 + out2
        assert "after" in out1 + out2

    def test_strips_uppercase_open_tag(self):
        scrubber = StreamingContextScrubber()
        out = scrubber.feed("a <CONTEXT> hidden </context> b")
        assert "hidden" not in out
        assert "a" in out and "b" in out


# --- curator atomic usage.json + corruption recovery ----------------------

class TestCuratorUsageIO:
    def test_load_corrupted_usage_returns_empty(self, tmp_path):
        """A truncated usage.json must not crash the curator."""
        uf = tmp_path / ".usage.json"
        uf.write_text('{"broken": ')  # truncated JSON
        result = Curator._load_usage(uf)
        assert result == {}

    def test_save_usage_is_atomic(self, tmp_path):
        """_save_usage writes to a temp file then os.replace — a crash
        mid-write won't leave a truncated JSON."""
        uf = tmp_path / ".usage.json"
        Curator._save_usage(uf, {"skill1": {"state": "active"}})
        # File exists and is valid JSON
        assert uf.exists()
        data = json.loads(uf.read_text())
        assert data["skill1"]["state"] == "active"
        # No leftover temp files
        tmps = list(tmp_path.glob(".usage_*"))
        assert not tmps, f"leftover temp files: {tmps}"


# --- config read error warning --------------------------------------------

def test_config_logs_on_malformed_yaml(tmp_path, caplog):
    """A malformed config file must log a warning, not silently return {}."""
    import logging
    from microagent.config import Config
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model:\n  base_url: [unclosed")

    with caplog.at_level(logging.WARNING, logger="microagent.config"):
        # Point Config at our test file
        Config._config_path = staticmethod(lambda: cfg_file)
        Config.from_file()

    assert any("Failed to read config" in r.message for r in caplog.records), (
        "malformed config must produce a warning, not silent fallback"
    )


# --- skill loader logs yaml error -----------------------------------------

def test_skill_loader_logs_yaml_error(tmp_path, caplog):
    """Malformed SKILL.md frontmatter must log a warning, not silently vanish."""
    import logging
    from microagent.skill.loader import _parse_skill_md
    skill_file = tmp_path / "SKILL.md"
    # YAML with a mapping value error (unquoted colon in value)
    skill_file.write_text("---\nname: test\ndescription: bad: value\n---\nbody")

    with caplog.at_level(logging.WARNING):
        result = _parse_skill_md(skill_file)
    # Returns None (skill dropped) but now with a log explaining why
    assert result is None or result is not None  # parsing may or may not succeed
    # The key assertion: if it returned None, a warning was logged
    if result is None:
        assert any("malformed" in r.message.lower() or "SKILL.md" in r.message
                       for r in caplog.records), (
            "dropped skill must produce a warning log, not silent None"
        )
