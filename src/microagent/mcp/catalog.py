"""MCP server catalog — pre-configured MCP servers for common use cases.

Each entry defines a human name, description, and the command-line
invocation needed to start the server. Use ``connect_mcp_stdio``
to connect and register a server's tools.

Adding a server here makes it discoverable without the user needing
to look up the correct npx/uvx command.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MCPServerSpec:
    """A single MCP server definition."""

    name: str
    description: str
    command: tuple[
        str, ...
    ]  # argv for subprocess (e.g. ("npx", "-y", "@modelcontextprotocol/server-filesystem"))


# ---------------------------------------------------------------------------
# Built-in catalog
# ---------------------------------------------------------------------------

BUILTIN_MCP_SERVERS: tuple[MCPServerSpec, ...] = (
    MCPServerSpec(
        name="filesystem",
        description="Read/write files with security boundaries. Requires a writable directory.",
        command=("npx", "-y", "@modelcontextprotocol/server-filesystem", "."),
    ),
    MCPServerSpec(
        name="git",
        description="Git repository operations — status, log, diff, branches.",
        command=("uvx", "mcp-server-git", "--repository", "."),
    ),
    MCPServerSpec(
        name="fetch",
        description="HTTP fetch with content extraction — better than raw curl.",
        command=("uvx", "mcp-server-fetch"),
    ),
    MCPServerSpec(
        name="postgres",
        description="PostgreSQL read-only queries. Set DATABASE_URL in env.",
        command=("npx", "-y", "@modelcontextprotocol/server-postgres"),
    ),
    MCPServerSpec(
        name="sqlite",
        description="SQLite read-only queries on a local .db file.",
        command=("uvx", "mcp-server-sqlite", "--db-path", "data.db"),
    ),
    MCPServerSpec(
        name="github",
        description="GitHub API — issues, PRs, repos. Requires GITHUB_PERSONAL_ACCESS_TOKEN.",
        command=("npx", "-y", "@modelcontextprotocol/server-github"),
    ),
    MCPServerSpec(
        name="brave-search",
        description="Web and local search via Brave Search API. Requires BRAVE_API_KEY.",
        command=("npx", "-y", "@modelcontextprotocol/server-brave-search"),
    ),
    MCPServerSpec(
        name="memory",
        description="Persistent knowledge graph memory (separate from MicroAgent built-in).",
        command=("npx", "-y", "@modelcontextprotocol/server-memory"),
    ),
    MCPServerSpec(
        name="puppeteer",
        description="Headless Chrome browser automation via Puppeteer.",
        command=("npx", "-y", "@modelcontextprotocol/server-puppeteer"),
    ),
    MCPServerSpec(
        name="sequential-thinking",
        description="Multi-step reasoning via sequential thought tool.",
        command=("npx", "-y", "@modelcontextprotocol/server-sequential-thinking"),
    ),
    MCPServerSpec(
        name="time",
        description="Time and timezone utilities.",
        command=("uvx", "mcp-server-time"),
    ),
    MCPServerSpec(
        name="everart",
        description="AI image generation via EverArt API. Requires EVERART_API_KEY.",
        command=("npx", "-y", "@modelcontextprotocol/server-everart"),
    ),
)


def get_server(name: str) -> MCPServerSpec | None:
    """Look up a server spec by name."""
    for spec in BUILTIN_MCP_SERVERS:
        if spec.name == name:
            return spec
    return None


def list_servers() -> list[dict[str, str]]:
    """Return a list of {name, description} for all catalogued servers."""
    return [{"name": s.name, "description": s.description} for s in BUILTIN_MCP_SERVERS]
