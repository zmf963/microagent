# MicroAgent — Development Guide

Instructions for AI coding assistants working on the microagent codebase.

## What MicroAgent Is

MicroAgent is an embeddable AI agent core library (~13,400 LOC, 34 tools, 1579 tests).
It runs the core agent loop — LLM → tool calls → LLM → text response — and
nothing else. No gateway, no desktop, no dashboard. It is a library, not a product.

The single most important principle: **the core is a narrow waist; capability
lives in tools and extension points, not in the core loop.** SessionRunner
(~1214 LOC) is the sole execution path. Everything else — memory, skills,
compression, permissions, subagents — plugs in through Protocols.

## Project Structure

```
microagent/
├── pyproject.toml          # hatchling build, 6 deps, 5 optional extras
├── src/microagent/
│   ├── __init__.py          # Public API surface (~68 symbols)
│   ├── agent.py             # Agent facade — from_config(), run(), arun()
│   ├── config.py            # Config resolution — CLI > env > file > default
│   ├── core/
│   │   ├── types.py         # Message, ToolCall, ToolResult, Usage, Event types
│   │   ├── tool.py          # @tool decorator, ToolRegistry, FunctionTool
│   │   ├── permission.py    # PermissionEngine, Rule, ScriptRule, DEFAULT_RULES
│   │   ├── store.py         # Store Protocol, SQLiteStore (WAL), InMemoryStore
│   │   └── event.py         # EventBus
│   ├── llm/
│   │   ├── client.py        # LLMConfig, OpenAIChatClient (delegates pricing)
│   │   ├── pricing.py       # models.dev cache (364 models), alias resolution
│   │   ├── models_cache.json # pricing seed file (shipped, offline-capable)
│   │   ├── templates.py     # Model-specific system prompt templates
│   │   └── pool.py          # CredentialPool — API key rotation
│   ├── session/
│   │   ├── runner.py        # SessionRunner — the core loop (~1214 LOC)
│   │   ├── compress.py      # 4-layer compression pyramid
│   │   ├── attachments.py   # File recovery after compaction
│   │   ├── budget.py        # Tree-shaped Budget with spawn/cancel_event
│   │   └── search.py        # FTS5 session search
│   ├── tools/builtins/      # 34 built-in tools
│   │   ├── read_file.py, write_file.py, edit_file.py, grep.py, glob.py
│   │   ├── bash.py, process.py
│   │   ├── web_search.py, web_fetch.py, context7.py
│   │   ├── browser.py        # 10 browser tools (Playwright)
│   │   ├── execute_code.py, vision_analyze.py
│   │   ├── task.py, todo_plan_exit.py
│   │   ├── skill_manage.py, session_search.py
│   │   ├── question.py, lsp.py, mcp_connect.py
│   │   ├── git.py, file_tree.py
│   ├── memory/              # MemoryProvider Protocol + SQLite + LLM extractor
│   ├── skill/               # SkillLoader + Curator lifecycle
│   ├── subagent/            # SubagentManager — task delegation
│   ├── plugin/types.py      # PreLLMHook, ToolHook, ContextSource Protocols
│   ├── terminal/            # LocalTerminal, DockerTerminal (+ SSH)
│   ├── mcp/                 # MCP stdio client
│   ├── cron/                # APScheduler cron integration
│   ├── currency.py          # USD→CNY display conversion (MICROAGENT_CURRENCY_RATE)
│   └── surface/cli.py       # Rich CLI with /slash commands (/models, /cost, …)
└── tests/
    ├── unit/                # unit tests (mock LLM, fast) — 91 files
    ├── smoke/               # import + lifecycle sanity checks
    ├── e2e/                 # full agent turns with tools (FakeLLM)
    ├── integration/         # real LLM API (MICROAGENT_TEST_* env vars)
    └── benchmark/           # performance benchmarks (marked, skipped by default)
    └── integration/         # 10 integration tests (real LLM API)
```

## Architecture Rules

### 1. The core loop is sacred

`SessionRunner.run_turn()` is the only execution path. It is ~1214 LOC and
every line traces to the core contract:

```
while not budget.exhausted:
    1. compress check (4-layer pyramid)
    2. skill matching → system prompt
    3. context sources + pre_llm_hooks
    4. LLM.stream(system, messages, tools)
    5. build assistant Message → auto-persist
    6. tool calls → concurrent TaskGroup → hook.before/after
    7. TurnComplete → memory extraction (fire-and-forget)
```

Do NOT add steps to this loop unless they are fundamental to every agent
execution. New capability should arrive as a Protocol implementation or a
tool, not as core loop logic.

### 2. Tools go in `tools/builtins/`, not in the core

Each tool is one file, one `@tool` decorator, one async function returning
`ToolResult`. Tools are auto-discovered via `_default_builtins()` lazy import.

```python
@tool("my_tool", description="Does something.")
async def my_tool(param: Annotated[str, Field(description="...")]) -> ToolResult:
    ...
```

Register in `_default_builtins()` (in `core/tool.py`) and add a `Rule` in
`DEFAULT_RULES` (in `core/permission.py`).

Tool conventions (deepseek-harness parity):
- Schema descriptions must NOT reference other tools by name — the model
  hallucinates calls to tools that don't exist in its current toolset.
- Tools that share per-session state (browser page, LSP servers) must be
  declared `exclusive=True` — the runner serializes them against each
  other (concurrency barrier).
- Tool execution is globally capped at 10 concurrent calls per turn
  (`MAX_PARALLEL_TOOL_CALLS` in `runner._run_tool_calls`).

### 3. Protocols > inheritance

Every extension point uses `typing.Protocol`:
- `LLMClient` — LLM providers
- `Store` — session persistence
- `MemoryProvider` — memory backends
- `SkillLoader` — skill matching
- `PreLLMHook`, `ToolHook`, `ContextSource` — plugin hooks

Do NOT add abstract base classes. Protocols are structural — any object
with the right methods satisfies the contract.

### 4. Prompt caching must not break

`SessionRunner` caches `_cached_system` and `_cached_tools` across turns.
Only rebuild when system prompt changes (skills loaded/unloaded). Anything
that mutates past context or rebuilds the system prompt mid-conversation
invalidates API-level cache and multiplies costs.

### 5. Compression is a 4-layer pyramid

```
L1: Micro-Compact (0 API)  — truncate re-obtainable tool results >500 chars
L2: Snip (0 API)           — remove oldest tool_result messages
L3: LLM Summary (1 call)   — 7-section structured summary + file attachments
L4: Circuit Breaker        — 3 failures → 300s cooldown
```

Incremental: `CompactionState.previous_summary` stores the last summary for
iterative updates (preserves old info, adds new).

Manual trigger: `/compact` in CLI, or `compact_conversation(force=True)` in API.

Auto trigger: `compression_threshold=0` → auto-computed as 60% of context window.

## Adding Features

### Adding a tool
1. Create `tools/builtins/your_tool.py` with `@tool()` + async function
2. Add lazy import to `_default_builtins()` in `core/tool.py`
3. Add `Rule("your_tool", {}, Decision.ALLOW)` to `DEFAULT_RULES`
4. Write tests in `tests/unit/test_your_tool.py`

### Adding a Protocol extension
1. Define the Protocol in `plugin/types.py` (or your module)
2. Pass implementations to `SessionRunner(pre_llm_hooks=...)`
3. Runner calls hooks at the defined points — no core changes needed

### Adding an optional extra
1. Add the dependency to `[project.optional-dependencies]` in pyproject.toml
2. Import lazily (try/except ImportError) — never fail at import time

## Testing

```bash
source .venv/bin/activate

# Unit tests (mock LLM, fast)
python -m pytest tests/unit/ -q            # 1122 unit tests
python -m pytest tests/unit/ tests/smoke/ tests/e2e/ -q   # 1579 tests total

# Integration tests (real LLM API)
MICROAGENT_TEST_BASE_URL=... \
MICROAGENT_TEST_API_KEY=... \
MICROAGENT_TEST_MODEL=oc-d4f \
python -m pytest tests/integration/ -v -m integration  # 7 tests

# Single test
python -m pytest tests/unit/test_runner.py::TestBudget::test_consume -v
```

### Test patterns
- Use `FakeLLMClient` with `text_response()` / `tool_response()` to script LLM behavior
- Use `InMemoryStore` for session persistence tests (no disk I/O)
- Use `tmp_path` fixture for file-based tests
- Integration tests auto-skip when `MICROAGENT_TEST_*` env vars are missing
- Never hardcode `~/.hermes/` or `~/.microagent/` paths — use fixtures

## Commit Style

```
feat: description              # new feature
fix: description               # bug fix
refactor: description          # code restructuring
test: description              # test additions
docs: description              # documentation
build: description             # packaging/metadata
```

Keep commits small and focused. One logical change per commit.

## Key Files for Common Changes

| Change | Files to edit |
|--------|--------------|
| New tool | `tools/builtins/*.py` + `core/tool.py` + `core/permission.py` + test |
| New LLM provider | `llm/client.py` (new class implementing Protocol) |
| Compression tuning | `session/compress.py` |
| CLI improvements | `surface/cli.py` |
| Session persistence | `core/store.py` + `session/runner.py` |
| New Protocol | `plugin/types.py` |
| Pricing/context window | `llm/pricing.py` (models.dev cache) + `llm/models_cache.json` (seed) |
| Currency display (CNY) | `currency.py` + `MICROAGENT_CURRENCY_RATE` env var |
| LLM failure handling | `llm/errors.py` (taxonomy) + `llm/watchdog.py` (idle timeout) |
| Store invariant audit | `MICROAGENT_AUDIT_INVARIANTS=1` env — runner asserts no orphaned tool_calls / consecutive users before each turn |
| Public API | `__init__.py` |
