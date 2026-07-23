# MicroAgent

[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A Python-implemented, embeddable universal AI agent core library. ~3k LOC, 12 built-in tools, 55 public API symbols.

> **Design philosophy**: "narrow waist + thick edges" — the core agent loop is <200 LOC, capability lives in tools and extension points, not in the core.

## Quick Start

```bash
pip install microagent
```

```python
import asyncio
from microagent import Agent, LLMConfig

async def main():
    agent = Agent.from_config(LLMConfig(
        base_url="https://api.openai.com/v1",
        api_key="sk-...",
        model="gpt-4o",
    ))
    result = await agent.arun("What is Python?")
    print(result)

asyncio.run(main())
```

Or use the CLI:

```bash
export MICROAGENT_BASE_URL="https://api.openai.com/v1"
export MICROAGENT_API_KEY="sk-..."
export MICROAGENT_MODEL="gpt-4o"
microagent "What is Python?"
```

## Architecture

```
user prompt → Agent → SessionRunner → LLM (OpenAI-compatible)
                         ↓ (tool calls)
                    ToolRegistry → 12 built-in tools
                         ↓
                    PermissionEngine → ALLOW / DENY / ASK
```

| Layer | What | LOC |
|-------|------|-----|
| `core/` | types, tool registry, permission engine, store, event bus | 685 |
| `llm/` | OpenAI-compatible client (SDK v2 streaming) | 184 |
| `session/` | runner loop, tree-shaped budget (spawn + cancel) | 351 |
| `tools/` | 12 built-in tools | 572 |
| `skill/` | Claude skill loader, composite loader, curator | 254 |
| `memory/` | FTS5-backed memory provider, LLM extractor | 176 |
| `subagent/` | subagent manager with isolated budgets | 151 |
| `terminal/` | local + docker + SSH backends | 159 |
| `mcp/` | MCP client (official SDK bridge) | 73 |
| `plugin/` | 3 extension Protocols + EventBus | 36 |
| `cron/` | APScheduler-based cron jobs | 100 |

## Built-in Tools (12)

| Tool | Description |
|------|-------------|
| `read_file` | Read files by line (offset/limit) |
| `write_file` | Write/overwrite files |
| `edit_file` | Find-and-replace in files |
| `bash` | Execute shell commands (local/docker/ssh) |
| `grep` | Regex search in files |
| `glob` | Find files by pattern |
| `web_fetch` | Fetch URL content |
| `todo` | Inline task list |
| `plan` | Multi-step planning |
| `task` | Spawn subagents (explore/general) |
| `skill_manage` | Runtime skill creation/modification |
| `exit` | End session |

## Extension Points

```python
from microagent import PreLLMHook, ToolHook, ContextSource, EventBus

# Transform system prompt before LLM call
class MyHook:
    async def __call__(self, ctx):
        return ctx  # modify and return

# Intercept tool calls
class AuditHook:
    async def before(self, call, ctx):
        return call  # return None to deny
    async def after(self, call, result, ctx):
        return result

# Inject extra context
class GitSource:
    async def contribute(self, ctx):
        return "git: main branch"

# Observe events
bus = EventBus()
bus.on("turn_complete", lambda sid, resp: print(f"Done: {resp[:50]}"))
```

## Optional Extras

| Extra | Install | Provides |
|-------|---------|----------|
| `mcp` | `pip install microagent[mcp]` | MCP client (official SDK) |
| `cron` | `pip install microagent[cron]` | APScheduler cron jobs |
| `tui` | `pip install microagent[tui]` | Textual terminal UI |
| `web` | `pip install microagent[web]` | FastAPI SSE streaming API |
| `ssh` | `pip install microagent[ssh]` | SSH terminal backend (paramiko) |
| `dev` | `pip install microagent[dev]` | pytest + pytest-asyncio |

## Key Features

- **OpenAI-compatible LLM abstraction** — any `/v1/chat/completions` endpoint (OpenAI, vLLM, Ollama, local-gateway)
- **Tree-shaped budget** — `Budget.root().spawn()` with shared cancel_event and descendants tracking
- **Self-improving learning loop** — `skill_manage` tool + `Curator` background lifecycle (Hermes-style)
- **Dual-track testing** — `FakeLLMClient` for unit tests + real API integration tests (`-m integration`)
- **FTS5 memory** — SQLite full-text search with zero external dependencies
- **Permission engine** — fnmatch rules + external script rules + ASK callback
- **Subagent system** — isolated contexts, filtered toolsets, independent budgets
- **Session persistence** — SQLite WAL store with checkpoint support
- **Skills dual ecosystem** — Claude Code skill format + composite loader

## Running Tests

```bash
# Unit tests (no network)
python -m pytest tests/unit/ -q

# Integration tests (requires real LLM API)
MICROAGENT_TEST_BASE_URL="..." \
MICROAGENT_TEST_API_KEY="..." \
MICROAGENT_TEST_MODEL="..." \
python -m pytest tests/integration/ -v -m integration
```

## License

MIT
