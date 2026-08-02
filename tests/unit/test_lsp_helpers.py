"""Tests for LSP pure helper functions and the lsp tool's fast error paths."""

import pytest


class TestDetectLang:
    def test_python(self):
        from microagent.tools.builtins.lsp import _detect_lang
        assert _detect_lang("file.py") == "python"

    def test_typescript(self):
        from microagent.tools.builtins.lsp import _detect_lang
        assert _detect_lang("file.ts") == "typescript"
        assert _detect_lang("file.tsx") == "typescript"
        assert _detect_lang("file.js") == "typescript"
        assert _detect_lang("file.jsx") == "typescript"

    def test_rust(self):
        from microagent.tools.builtins.lsp import _detect_lang
        assert _detect_lang("lib.rs") == "rust"

    def test_go(self):
        from microagent.tools.builtins.lsp import _detect_lang
        assert _detect_lang("main.go") == "go"

    def test_c_cpp(self):
        from microagent.tools.builtins.lsp import _detect_lang
        for ext in (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx"):
            assert _detect_lang(f"file{ext}") == "cpp", ext

    def test_unknown(self):
        from microagent.tools.builtins.lsp import _detect_lang
        assert _detect_lang("file.txt") == ""
        assert _detect_lang("noext") == ""


class TestFindLSPCommand:
    def test_returns_tuple_for_python(self, monkeypatch):
        from microagent.tools.builtins import lsp as lsp_mod
        # Monkeypatch shutil.which to simulate installed servers
        def _fake_which(cmd):
            return f"/usr/bin/{cmd}"
        monkeypatch.setattr(lsp_mod.shutil, "which", _fake_which)
        cmd = lsp_mod._find_lsp_command("python")
        assert cmd is not None
        assert isinstance(cmd, tuple)
        assert cmd[0] == "pyright-langserver" or "pyright" in cmd[0]

    def test_returns_none_when_missing(self, monkeypatch):
        from microagent.tools.builtins import lsp as lsp_mod
        monkeypatch.setattr(lsp_mod.shutil, "which", lambda cmd: None)
        assert lsp_mod._find_lsp_command("python") is None

    def test_unknown_lang(self):
        from microagent.tools.builtins.lsp import _find_lsp_command
        assert _find_lsp_command("nonexistent-lang") is None


class TestSymbolHelpers:
    def test_symbol_kind_name_known(self):
        from microagent.tools.builtins.lsp import _symbol_kind_name
        assert _symbol_kind_name(5) == "class"
        assert _symbol_kind_name(12) == "function"

    def test_symbol_kind_name_unknown(self):
        from microagent.tools.builtins.lsp import _symbol_kind_name
        assert _symbol_kind_name(999) == "symbol(999)"

    def test_is_anonymous_symbol(self):
        from microagent.tools.builtins.lsp import _is_anonymous_symbol
        assert _is_anonymous_symbol("(anonymous struct)") is True
        assert _is_anonymous_symbol("(anonymous namespace)") is True
        assert _is_anonymous_symbol("real_symbol") is False


class TestLSPToolErrorPaths:
    @pytest.mark.asyncio
    async def test_lsp_no_language(self, tmp_path):
        """A file with no recognized language returns an error."""
        from microagent.tools.builtins.lsp import lsp
        f = tmp_path / "file.unknownextension"
        f.write_text("content")
        r = await lsp.fn(
            action="symbols",
            filepath=str(f),
        )
        assert r.is_error
        assert "language" in r.content.lower() or "lsp" in r.content.lower()

    @pytest.mark.asyncio
    async def test_lsp_unknown_action(self, tmp_path):
        from microagent.tools.builtins.lsp import lsp
        f = tmp_path / "test.py"
        f.write_text("x = 1\n")
        r = await lsp.fn(action="bogus", filepath=str(f))
        assert r.is_error

    @pytest.mark.asyncio
    async def test_lsp_missing_file(self):
        from microagent.tools.builtins.lsp import lsp
        r = await lsp.fn(action="symbols", filepath="/nonexistent/file.py")
        assert r.is_error
