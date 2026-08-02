"""Tests for microagent.mcp.catalog — the built-in MCP server registry."""

import pytest

from microagent.mcp.catalog import (
    BUILTIN_MCP_SERVERS,
    MCPServerSpec,
    get_server,
    list_servers,
)


class TestCatalogContents:
    def test_has_expected_servers(self):
        names = {s.name for s in BUILTIN_MCP_SERVERS}
        for expected in ("filesystem", "git", "fetch", "sqlite", "time", "github"):
            assert expected in names, f"missing {expected}"

    def test_all_entries_are_specs(self):
        assert all(isinstance(s, MCPServerSpec) for s in BUILTIN_MCP_SERVERS)

    def test_no_duplicate_names(self):
        names = [s.name for s in BUILTIN_MCP_SERVERS]
        assert len(names) == len(set(names))

    def test_each_spec_has_command_tuple(self):
        for s in BUILTIN_MCP_SERVERS:
            assert isinstance(s.command, tuple)
            assert len(s.command) >= 1, f"{s.name} has empty command"
            assert isinstance(s.command[0], str)


class TestGetServer:
    def test_finds_existing(self):
        spec = get_server("git")
        assert spec is not None
        assert spec.name == "git"
        assert spec.command[0] == "uvx"

    def test_returns_none_for_unknown(self):
        assert get_server("nonexistent-server") is None

    def test_case_sensitive(self):
        assert get_server("Git") is None


class TestListServers:
    def test_returns_list_of_dicts(self):
        result = list_servers()
        assert isinstance(result, list)
        assert len(result) == len(BUILTIN_MCP_SERVERS)
        for item in result:
            assert set(item.keys()) == {"name", "description"}

    def test_matches_catalog_names(self):
        names = {item["name"] for item in list_servers()}
        assert names == {s.name for s in BUILTIN_MCP_SERVERS}


class TestSpecImmutability:
    def test_spec_is_frozen(self):
        spec = BUILTIN_MCP_SERVERS[0]
        with pytest.raises(Exception):
            spec.name = "renamed"
