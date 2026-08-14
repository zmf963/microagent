# MicroAgent

[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-1175%20passed-brightgreen.svg)]()

A Python-implemented, embeddable universal AI agent core library.
**~12,600 LOC**, **34 built-in tools**, **62 public API symbols**, **1175 unit+smoke+e2e tests, 3 integration tests**.

> *"narrow waist + thick edges"* — the core agent loop (`SessionRunner.run_turn`) is one focused method. Capability lives in tools and extension points, not in the core.

---

## Quick Start

### Installation

```bash
pip install microagent
```

### First Agent

```python
import asyncio
from microagent import Agent, LLMConfig

async def main():
    agent = Agent.from_config(LLMConfig(
        base_url="https://api.openai.com/v1",
        api_key="sk-...",
        model="gpt-4o",
    ))
    result = await agent.arun("What is the capital of France?")
    print(result)

asyncio.run(main())
```

Any OpenAI-compatible endpoint works: vLLM, Ollama, local-gateway, OpenRouter, etc.

### CLI

```bash
# Set credentials
export MICROAGENT_BASE_URL="https://api.openai.com/v1"
export MICROAGENT_API_KEY="sk-..."
export MICROAGENT_MODEL="gpt-4o"

# One-shot
microagent "What is Python?"

# Interactive REPL
microagent
>>> list files in current directory
>>> write a hello.py script
>>> /models              # show current model pricing
>>> /models gpt-4o-mini  # look up any model's price
>>> /cost                # session token + cost summary
>>> /exit
```

Cost is displayed in CNY (¥) by default. Override the exchange rate:
```bash
export MICROAGENT_CURRENCY_RATE=7.35  # CNY per 1 USD (default 7.20)
```

### CLI Commands

| Command | Description |
|---------|-------------|
| `/new` | Start a new session |
| `/list` | List saved sessions |
| `/resume [id]` | Resume last or specific session |
| `/compact` | Manually compress current conversation |
| `/model [name]` | Show or switch model |
| `/models [name\|refresh\|count]` | Show model pricing (CNY), refresh cache, or count |
| `/cost` | Show session token usage + cost summary |
| `/history` | Show message history for current session |
| `/skill list\|load\|unload` | Manage skills |
| `/plan` | Switch to plan mode (read-only tools) |
| `/build` | Switch to build mode (all tools) |
| `/thinking [on\|off]` | Toggle reasoning/thinking display |
| `/clear` | Clear the screen |
| `/help` | Show available commands |

---

## Usage Guide

### Basic Usage

```python
from microagent import Agent, LLMConfig, Message

agent = Agent.from_config(LLMConfig(
    base_url="https://api.openai.com/v1",
    api_key="sk-...",
    model="gpt-4o",
))

# Simple string input → string output
result = await agent.arun("Read pyproject.toml and summarize it.")
print(result)

# Structured messages
messages = [
    Message.user("What is Python?"),
]
result = await agent.arun(messages)

# With system prompt
agent2 = Agent.from_config(
    LLMConfig(base_url="...", api_key="...", model="gpt-4o"),
    system_prompt="You are a Python expert. Always give code examples.",
)
```

### Working with Messages

```python
from microagent import Message, ToolCall

# User message
msg = Message.user("hello")

# Assistant message with tool calls
msg = Message.assistant(
    text="Let me read that file.",
    tool_calls=(ToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),),
)

# Tool result message
from microagent import ToolResult
msg = Message.tool_result(
    ToolResult.ok("file contents here"),
    tool_call_id="c1",
)
```

### Streaming

```python
from microagent import SessionRunner, ToolRegistry
from microagent.core.types import TextDelta, TurnComplete

runner = SessionRunner(llm=client, registry=ToolRegistry())
messages = [Message.user("Write a poem.")]

async for event in runner.run_turn(messages):
    if isinstance(event, TextDelta):
        print(event.text, end="", flush=True)  # real-time output
    elif isinstance(event, TurnComplete):
        print(f"\n--- done, full response: {event.content}")
```

### Permissions

> **Library extension point.** `PermissionEngine` is NOT wired into `SessionRunner` by default — tool calls bypass permission enforcement unless a library user wires it in via a `ToolHook.before`. The CLI runs without permission enforcement.

```python
from microagent import PermissionEngine, Rule, Decision, ScriptRule

# Rule-based: fnmatch on tool name + argument constraints
engine = PermissionEngine(rules=(
    Rule("read_file", {}, Decision.ALLOW),                    # always allow reads
    Rule("bash", {"command": "ls *"}, Decision.ALLOW),        # allow ls only
    Rule("bash", {}, Decision.ASK),                           # ask for other commands
    Rule("write_file", {}, Decision.DENY),                    # block all writes
))

# External script rule: delegates to a Python script (stdout "allow"/"deny")
engine = PermissionEngine(rules=(
    ScriptRule("*", {}, script="./audit.py", timeout=5.0),   # every tool call
))

# ASK callback for interactive prompts
async def ask_user(call, rule):
    answer = input(f"Allow {call.name}? [y/N] ")
    return Decision.ALLOW if answer.lower() == "y" else Decision.DENY

engine = PermissionEngine(rules=(...), ask_callback=ask_user)

# Wire into SessionRunner via a ToolHook:
class PermissionHook:
    def __init__(self, engine): self.engine = engine
    async def before(self, call, ctx):
        decision = await self.engine.evaluate(call, ctx)
        return call if decision.decision is Decision.ALLOW else None  # None denies
    async def after(self, call, result, ctx): return result

runner = SessionRunner(llm=..., registry=..., tool_hooks=(PermissionHook(engine),))
```

### Subagents

```python
from microagent import SubagentManager, SubagentSpec

# Use default subagents (explore + general)
mgr = SubagentManager()

# Spawn a read-only code explorer
result = await mgr.spawn("explore", "Find all TODO comments", parent_runner)

# Custom subagent
mgr2 = SubagentManager(specs=(
    SubagentSpec(
        name="code-reviewer",
        description="Reviews code for bugs",
        system_prompt="You review code. Be thorough.",
        tools_allowed=("read_file", "grep", "glob"),
        tools_blocked=("write_file", "bash"),
        max_iterations=15,
        max_cost_usd=0.5,
    ),
))
```

### Memory

```python
from microagent import SQLiteMemoryProvider, Memory

store = SQLiteMemoryProvider("~/.microagent/memory.db")

# Write memories
await store.batch_write((
    Memory(id="m1", content="User prefers Python.", category="preference", created_at=time.time()),
    Memory(id="m2", content="Project is at /home/user/app.", category="fact", created_at=time.time()),
))

# FTS5 full-text search
results = await store.recall("Python project", k=5)
for r in results:
    print(f"[{r.category}] {r.content} (score: {r.relevance_score:.2f})")

# Delete
await store.delete("m1")
store.close()
```

### Skills

```python
from microagent import ClaudeSkillLoader, CompositeSkillLoader

# Load skills from ~/.claude/skills/<name>/SKILL.md
loader = ClaudeSkillLoader(search_paths=("~/.claude/skills",))
skills = await loader.load()

# Match skills by user input (keyword triggers + fuzzy description matching)
matches = await loader.match("I need to search deeply for information")
for m in matches:
    print(f"{m.skill.name}: {m.match_reason} (score: {m.match_score})")

# Combine multiple loaders, deduplicate, rank by score
composite = CompositeSkillLoader(backends=(claude_loader, custom_loader))
top = await composite.match("deploy to production")
```

### Budget Tree

```python
from microagent import Budget, BudgetExceeded

# Root budget: total session limits
root = Budget.root(max_iterations=50, max_tokens=500_000, max_cost_usd=10.0)

# Spawn child budget (inherits 1/3 of parent remaining by default)
child = root.spawn(max_iterations=10, max_cost_usd=1.0)

# Child consumption reports to all ancestors
child.consume(iterations=2, tokens=1500, cost_usd=0.05)
print(f"Root remaining cost: ${root.remaining_cost:.2f}")  # $9.95

# Note: budget limits and cost tracking are internally in USD.
# Use microagent.currency.format_cost() for CNY (¥) display conversion.
from microagent.currency import format_cost
print(f"Remaining: {format_cost(root.remaining_cost)}")  # ¥71.64

# Root exhaustion cancels all descendants
root.consume(iterations=50, cost_usd=10.0)  # raises BudgetExceeded
try:
    child.consume(iterations=1)             # raises "budget cancelled by root"
except BudgetExceeded:
    print("Cancelled")
```

### Extension Points

```python
from microagent import PreLLMHook, ToolHook, ContextSource, EventBus

# Transform the system prompt string before the LLM call
class AddContextHook:
    async def __call__(self, ctx):
        return ctx + "\nAdditional context here."

# Intercept tool calls (return None from before() to deny)
class AuditHook:
    async def before(self, call, ctx):
        print(f"About to run: {call.name}({call.arguments})")
        return call  # return None to deny

    async def after(self, call, result, ctx):
        print(f"Result: {result.content[:100]}")
        return result

# Inject extra content into the USER message (per ADR-0005 the system
# prompt is frozen; ContextSource appends to the user turn)
class GitSource:
    async def contribute(self, ctx):
        return f"\ngit: main branch, 3 files changed"

# Observe events
bus = EventBus()
bus.on("turn_complete", lambda sid, resp: log_to_file(sid, resp))

runner = SessionRunner(
    llm=client,
    registry=ToolRegistry(),
    pre_llm_hooks=(AddContextHook(),),
    tool_hooks=(AuditHook(),),
    context_sources=(GitSource(),),
    event_bus=bus,
)
```

### Session Persistence

```python
from microagent import SQLiteStore, InMemoryStore, SessionRunner

# SQLite WAL store
store = SQLiteStore("sessions.db")
await store.append("session-1", Message.user("hello"))
await store.append("session-1", Message.assistant("hi"))
await store.checkpoint("session-1")

# Resume session later
runner = SessionRunner(llm=client, registry=ToolRegistry())
history = await runner.resume("session-1", store)
# Continue conversation
messages = list(history) + [Message.user("what did we talk about?")]
async for event in runner.run_turn(messages):
    ...

store.close()
```

### MCP Client

```bash
pip install microagent[mcp]
```

```python
from microagent import connect_mcp_stdio, ToolRegistry

registry = ToolRegistry()
await connect_mcp_stdio(("uvx", "mcp-server-git"), registry)
# MCP server's tools are now registered as MicroAgent tools
print(registry.names)  # e.g., ['git_status', 'git_diff', ...]
```

### Cron Jobs

```bash
pip install microagent[cron]
```

```python
from microagent import CronScheduler, CronJob

scheduler = CronScheduler(agent=agent)
scheduler.add_job(CronJob(
    name="daily-summary",
    schedule="0 9 * * *",     # cron expression
    prompt="Summarize today's activity.",
))
scheduler.add_job(CronJob(
    name="health-check",
    schedule="interval:300",  # every 5 minutes
    prompt="Check if all services are running.",
))
scheduler.start()
# ... app runs ...
await scheduler.stop()
```

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Surface Layer (Rich CLI with /slash commands)  │
├─────────────────────────────────────────────────┤
│  Plugin Layer (PreLLMHook / ToolHook / Context) │
├─────────────────────────────────────────────────┤
│  Session Runner (LLM → tools → LLM loop)        │
│  ┌──────────┬──────────┬──────────┬──────────┐ │
│  │ Tools    │ Skills   │ Memory   │ Subagent │ │
│  │ Registry │ Loader   │ Provider │ Manager  │ │
│  └──────────┴──────────┴──────────┴──────────┘ │
├─────────────────────────────────────────────────┤
│  Core (types / permission / store / event /     │
│        budget tree / LLM client / pricing)      │
└─────────────────────────────────────────────────┘
```

| Module | Files | LOC | Description |
|--------|-------|-----|-------------|
| `core/` | 6 | 1203 | types, tool registry, permission, store, event bus |
| `tools/` | 28 | 3494 | 34 built-in tools (read, write, bash, grep, browser, lsp, mcp, etc.) + session-state helper |
| `session/` | 6 | 2439 | runner loop, 4-layer compression, budget, attachments, search |
| `memory/` | 3 | 520 | FTS5 memory provider, LLM extractor |
| `skill/` | 3 | 455 | Claude skill loader, curator lifecycle |
| `surface/` | 2 | 962 | Rich CLI REPL with /slash commands (/models, /cost, /compact, …) |
| `llm/` | 5 | 812 | OpenAI client, credential pool, models.dev pricing cache, templates |
| `terminal/` | 2 | 367 | local + docker + SSH backends (library extension point) |
| `mcp/` | 3 | 305 | MCP stdio client + catalog |
| `cron/` | 2 | 345 | APScheduler-based cron jobs |
| `security/` | 3 | 181 | streaming context scrubber, injection patterns (library extension point) |
| `subagent/` | 2 | 193 | subagent manager with isolated budgets |
| `plugin/` | 2 | 46 | 3 extension Protocols (PreLLMHook, ToolHook, ContextSource) |
| top-level | 3 | 367 | Agent facade, Config resolver, currency helper |

## Built-in Tools

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents with line numbers (offset/limit, 1-indexed) |
| `write_file` | Write/overwrite files (creates parent directories) |
| `edit_file` | Find-and-replace in files (single or replace_all) |
| `bash` | Execute shell commands via local/docker/SSH backends |
| `grep` | Regex search in files with line numbers |
| `glob` | Find files by glob pattern, sorted output |
| `process` | Manage background processes (start/poll/kill/wait/log/write/list) |
| `web_search` | Search the web via DuckDuckGo lite |
| `web_fetch` | Fetch URL content via httpx (SSRF-protected) |
| `context7` | Fetch up-to-date documentation via Context7 API |
| `browser_navigate` | Open a URL in Playwright browser |
| `browser_snapshot` | Get a text snapshot of the current page (full or compact mode) |
| `browser_click` | Click an element by CSS selector, ref ID, or text content |
| `browser_type` | Type text into an input field by CSS selector |
| `browser_back` | Navigate back to the previous page |
| `browser_scroll` | Scroll the page up/down by pixel amount |
| `browser_press` | Press keyboard keys (Enter, Tab, Escape, ArrowDown, etc.) |
| `browser_console` | Read console messages or evaluate JavaScript |
| `browser_get_images` | List visible images with URLs, alt text, dimensions |
| `browser_vision` | Full-page PNG screenshot with optional element annotations |
| `execute_code` | Execute Python code in a subprocess sandbox |
| `lsp` | Language Server Protocol client (Python/TypeScript/Rust/Go/C++) |
| `vision_analyze` | Analyze images via base64 + vision API |
| `session_search` | Search past conversation history (FTS5) |
| `task` | Spawn subagents (explore: read-only, general: multi-step) |
| `todo` | Manage inline task list (list/add/update/remove) |
| `task_plan` | Create multi-step plans without executing |
| `question` | Ask user for input (non-blocking, asyncio.to_thread) |
| `skill_manage` | Runtime skill creation/patching/deletion |
| `skills_list` | List all available skills |
| `git` | Git operations (status, diff, log, commit) |
| `file_tree` | Directory tree visualization |
| `mcp_connect` | Connect to MCP server at runtime |
| `exit` | Signal task completion |

## Optional Extras

| Extra | Command | Provides |
|-------|---------|----------|
| `mcp` | `pip install microagent[mcp]` | MCP client (official SDK, stdio transport) |
| `cron` | `pip install microagent[cron]` | APScheduler cron jobs |
| `ssh` | `pip install microagent[ssh]` | SSH terminal backend via paramiko |
| `browser` | `pip install microagent[browser]` | Playwright browser automation |
| `dev` | `pip install microagent[dev]` | pytest + pytest-asyncio |

## Running Tests

```bash
# Unit tests (mock LLM, no network)
python -m pytest tests/unit/ -q          # 1122 unit tests

# Smoke tests (fast import + lifecycle sanity)
python -m pytest tests/smoke/ -q          # 11 smoke tests

# End-to-end tests (full agent turns with tools)
python -m pytest tests/e2e/ -q            # 9 e2e tests

# All fast tests
python -m pytest tests/unit/ tests/smoke/ tests/e2e/ -q
# 1175 passed, 1 skipped

# Integration tests (requires real LLM API)
MICROAGENT_TEST_BASE_URL="http://your-endpoint/v1" \
MICROAGENT_TEST_API_KEY="sk-..." \
MICROAGENT_TEST_MODEL="your-model" \
python -m pytest tests/integration/ -v -m integration   # 10 tests

# Test coverage (requires: pip install coverage)
python -m coverage run --source=src/microagent -m pytest tests/unit/ tests/smoke/ tests/e2e/ -q
python -m coverage report                                # ~82% line coverage
```

## Key Features

- **OpenAI-compatible LLM** — any `/v1/chat/completions` endpoint (vLLM, Ollama, local-gateway, OpenRouter, etc.)
- **Accurate cost tracking** — models.dev pricing cache (364 models, auto-refreshable), CNY (¥) display via `MICROAGENT_CURRENCY_RATE`, `/models` + `/cost` CLI commands
- **34 built-in tools** — read/write/edit, bash, grep/glob, browser automation (10 Playwright tools), LSP (Python/TS/Rust/Go/C++), web search/fetch, MCP connect, vision, session search, todo/plan, and more
- **Tree-shaped budget** — `spawn()` with shared cancel_event, descendants tracking, `consume_usage()` helper
- **4-layer compression** — micro-compact → snip → LLM summary → circuit breaker; incremental summaries with file-attachment recovery
- **Self-improving loop** — `skill_manage` tool + `Curator` lifecycle (Hermes-style)
- **Subagent system** — isolated contexts, filtered toolsets, independent budgets, recursion-guarded
- **Session persistence** — SQLite WAL store with checkpoint + resume + FTS5 search
- **Skills dual ecosystem** — Claude Code SKILL.md format + composite loader with CJK-aware fuzzy matching
- **Permission engine** — fnmatch rules + ScriptRule + ASK callback (library extension point, requires manual wiring)
- **Extension points** — 3 Protocols (PreLLMHook, ToolHook, ContextSource) + EventBus (zero overhead when unused)
- **Dual-track testing** — `FakeLLMClient` (1122 unit tests) + real API (integration)

## License

MIT
