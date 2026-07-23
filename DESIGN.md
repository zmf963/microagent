# MicroAgent 设计说明

> 版本 0.1.0 | Python ≥3.14 | ~4,700 行核心代码 | 22 内置工具 | 215 测试

## 一、定位与设计原则

MicroAgent 是一个**可嵌入的通用 AI Agent 核心库**。它不是产品（不像 Hermes Agent 附带 Gateway/Desktop/Dashboard），而是一个可以嵌入任何 Python 应用的 Agent 运行时。

### 核心原则

1. **最小内核** — 核心循环 LLM→Tool→LLM 是唯一必选路径，所有能力通过 Protocol 扩展
2. **零依赖核心** — 必装仅 5 个（openai、pydantic、anyio、httpx、pyyaml），其余 optional
3. **TDD 全覆盖** — 215 测试，red-green-refactor 驱动
4. **信息分通道** — 借鉴 Claude Code，压缩走 4 层金字塔

### 架构全景

```
┌──────────────────────────────────────────────────────┐
│                 Surface Layer                         │
│  CLI (ANSI boxed)  │  TUI (textual)  │  Web (FastAPI)│
└──────────────────────┬───────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────┐
│                 Agent Facade                          │
│  Agent.from_config()  │  Agent.run(text)              │
└──────────────────────┬───────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────┐
│              SessionRunner (核心循环)                  │
│                                                       │
│  while not budget.exhausted:                          │
│    1. 压缩检查 (4层金字塔, context window 感知)        │
│    2. Skill 匹配 → system prompt                      │
│    3. LLM 调用 (streaming, prompt caching)            │
│    4. Tool 执行 (并発 TaskGroup, 权限检查)             │
│    5. 自动持久化 (SQLite WAL)                         │
│    6. Memory 提取 (fire-and-forget)                   │
└──────────────────────┬───────────────────────────────┘
                       │
    ┌──────────────────┼──────────────────┐
    ▼                  ▼                  ▼
┌──────────┐  ┌──────────────┐  ┌──────────────┐
│ LLM 层   │  │  Tool 层     │  │ Session 层   │
│          │  │              │  │              │
│ OpenAI   │  │ 22 tools     │  │ SQLite WAL   │
│ CredPool │  │ Permission   │  │ 4层压缩      │
│ Cost calc│  │ ScriptRule   │  │ 树形 Budget  │
│ Context  │  │ Pydantic     │  │ resume/search│
│ window   │  │ schema       │  │              │
└──────────┘  └──────────────┘  └──────────────┘
    │              │
    ▼              ▼
┌──────────┐  ┌──────────────┐
│ Memory   │  │  Skill       │
│          │  │              │
│ FTS5     │  │ ClaudeLoader │
│ LLM ext  │  │ Curator      │
│ recall   │  │ create/patch │
└──────────┘  └──────────────┘
```

---

## 二、核心类型 (`core/types.py` — 152 行)

```python
@dataclass(frozen=True, slots=True)
class Message:
    role: str                           # user | assistant | tool
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    usage: Usage | None = None
    is_error: bool = False              # 错误标记（压缩保护）

@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

class ToolResult:
    content: str; is_error: bool; metadata: dict | None

@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int; output_tokens: int; cost_usd: float
```

**Event 类型**（SessionRunner 流式输出）：
- `TextDelta(text, kind)` — 流式文本，kind=`"thinking"`|`"content"`
- `ToolCallDelta(id, name, arguments)` — 完整工具调用
- `ToolResultDelta(id, name, content, is_error)` — 工具结果摘要
- `TurnComplete(content)` — 对话轮次结束
- `TurnFailed(reason)` — 预算耗尽/错误

---

## 三、工具系统 (`core/tool.py` — 220 行)

```python
@tool("read_file", description="Read a file...")
async def read_file(path, offset=1, limit=500) -> ToolResult: ...

registry = ToolRegistry(_default_builtins())   # 22 内置工具
registry.to_openai_tools()                     # → OpenAI function schema
```

### 22 内置工具

| 分类 | 工具 | 说明 |
|------|------|------|
| 文件 | `read_file`, `write_file`, `edit_file`, `grep`, `glob` | 文件 I/O |
| 终端 | `bash`, `process` | 前台 + 后台进程管理 |
| 网络 | `web_search`, `web_fetch`, `context7` | 搜索 + 文档 |
| 浏览器 | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type` | Playwright |
| 代码 | `execute_code` | 子进程沙箱 |
| 视觉 | `vision_analyze` | base64 + vision API |
| 会话 | `session_search` | FTS5 历史搜索 |
| 代理 | `task`, `todo`, `plan`, `exit` | 子代理 + 计划 |
| 技能 | `skill_manage` | create/patch/list/delete |

### 权限引擎 (`core/permission.py` — 190 行)

```python
DEFAULT_RULES = (
    Rule("read_file", {}, Decision.ALLOW),
    Rule("bash", {}, Decision.ASK_USER),
    Rule("edit_file", {"glob": "*.py"}, Decision.ALLOW),
    ScriptRule("bash", {}, "/usr/local/bin/approver.sh"),
)
```

支持 `Rule`（基于 tool_name + pattern 匹配）和 `ScriptRule`（外部脚本决策）。

---

## 四、LLM 层 (`llm/client.py` — 260 行)

### LLMConfig

```python
@dataclass(frozen=True, slots=True)
class LLMConfig:
    base_url: str
    api_key: ***
    model: str
    reasoning_effort: str | None = None   # o-series: low/medium/high
    service_tier: str | None = None       # auto/default/flex
```

### OpenAIChatClient

- 异步流式调用 openai SDK v2
- `reasoning_content` → `TextDelta(kind="thinking")`
- `delta.content` → `TextDelta(kind="content")`
- tool call delta 累积 → `ToolCallDelta`
- 支持 `CredentialPool` API key 轮转

### 模型感知

```python
get_context_window("oc-d4f") → 200_000      # 12 模型窗口表
_estimate_cost("gpt-4o", 1000, 500) → $0.0075  # 11 模型定价
```

---

## 五、会话管理 (`session/`)

### SessionRunner (`runner.py` — 277 行)

核心循环：

```
while not budget.exhausted:
    ├─ 压缩检查 → L1→L2→L3→L4 (context window 感知)
    ├─ Skill 匹配 → 注入 system prompt
    ├─ Context sources + pre_llm_hooks
    ├─ LLM.stream(system, messages, tools) ← prompt caching
    ├─ 构建 assistant Message → 自动持久化
    ├─ 工具执行 → 并发 TaskGroup → hook.before/after
    └─ TurnComplete → memory extraction (fire-and-forget)
```

关键设计：
- **prompt caching**：system prompt + tools schema 缓存，字节稳定前缀
- **自动持久化**：user/assistant/tool_result 自动写入 store
- **压缩**：`compression_threshold=0` 时自动用 60% 窗口计算

### 4 层压缩金字塔 (`compress.py` — 320 行)

| 层 | 名称 | API | 触发 | 操作 |
|----|------|-----|------|------|
| L1 | Micro-Compact | 零 | 60% 窗口 | 裁剪工具结果 >500 chars |
| L2 | Snip | 零 | 80% 窗口 | 删除最早 tool_result |
| L3 | LLM 摘要 | 一次 | 窗口-8k | 7 章节结构化摘要 |
| L4 | 熔断 | 零 | 连续 3 次失败 | 300s 冷却 + 占位符 |

**7 章节摘要模板**：请求和意图 | 技术决策 | 文件和代码 | 错误修复 | 所有用户消息 | 待办 | 当前进度

### SQLiteStore (`core/store.py` — 170 行)

```python
store = SQLiteStore("~/.microagent/sessions.db")  # WAL 模式
await store.append(session_id, message)            # JSON 序列化
await store.load_history(session_id)               # → list[Message]
await store.list_sessions()                        # → list[str]
```

### Budget 树形预算 (`session/budget.py` — 168 行)

```python
root = Budget.root(max_iterations=25, max_tokens=200_000, max_cost_usd=5.0)
child = root.spawn()  # 继承 1/3 剩余配额
root.consume(iterations=1, tokens=1000, cost_usd=0.01)
# → 向上传播到祖先 → exhausted 时 cancel_event 通知子树
```

---

## 六、记忆系统 (`memory/`)

- **SQLiteMemoryProvider** — FTS5 全文本索引，`recall(query, k)` 语义搜索
- **MemoryExtractor** — LLM 提取结构化事实，fire-and-forget 异步

---

## 七、技能系统 (`skill/`)

- **ClaudeSkillLoader** + **CompositeLoader** — keyword + fuzzy 匹配
- **SkillManager** — create/patch/list/delete，双生态（内置 + 用户）
- **Curator** — 生命周期管理：active → stale → archived，pinned 保护

---

## 八、扩展点 (`plugin/types.py`)

```python
class PreLLMHook(Protocol):
    async def __call__(self, system_prompt: str) -> str: ...

class ToolHook(Protocol):
    async def before(self, call, ctx) -> ToolCall | None: ...
    async def after(self, call, result, ctx) -> ToolResult: ...

class ContextSource(Protocol):
    async def contribute(self, ctx) -> str: ...
```

---

## 九、Surface 层

### CLI (`surface/cli.py` — 305 行)

```
────── 💭 thinking ──────      思考过程（灰色分割线）

╭─ 🔧 tool ─────────────╮     工具调用（青色框）
│  args                   │
╰─ ✓ result ─────────────╯     工具结果（绿色 √）

直接流式输出                     正文

>>> /new /list /resume /help    会话命令
```

- ANSI 颜色 + Unicode box drawing
- 动态终端宽度（CJK 感知）
- 默认持久化到 `~/.microagent/sessions.db`

### TUI (`surface/tui.py`) / Web (`surface/web.py`)
- textual / FastAPI + SSE，均为 optional extras

---

## 十、配置系统

**优先级**：CLI 参数 > 环境变量 > `~/.microagent/config.yaml` > 默认值

```yaml
# ~/.microagent/config.yaml
model:
  base_url: "http://10.144.0.2:20128/v1"
  api_key: "sk-xxx"
  model: "oc-d4f"
system_prompt: "你是一个Python专家。"
```

---

## 十一、测试覆盖

```
215 tests, 1 skipped, 0 failures  —  2,608 / 4,708 = 55% test/code ratio
```

| 测试文件 | 覆盖 |
|---------|------|
| test_runner.py | SessionRunner, Budget |
| test_builtins.py | 22 工具注册 |
| test_compression.py + test_compaction_pyramid.py | 4 层压缩 |
| test_session_persist.py | 会话持久化 |
| test_process.py | 进程管理 |
| test_browser.py / test_context7.py / test_vision.py | 各工具 |
| test_config.py / test_permission.py | 配置/权限 |
| test_credential_pool.py / test_memory_extractor.py | LLM 层 |

---

## 十二、与 Hermes/Claude Code/OpenCode 对比

| 能力 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| 核心循环 | ✅ | ✅ | ✅ |
| 工具数 | 22 | 30+ | 10+ |
| 压缩金字塔 | ✅ 4 层 | ✅ ContextCompressor | ✅ 5 层 |
| 会话持久化 | ✅ SQLite WAL | ✅ SessionDB | ✅ transcript |
| 记忆系统 | ✅ FTS5+LLM | ✅ 8 providers | — |
| 技能系统 | ✅ 双生态+Curator | ✅ skills+hub | — |
| 树形预算 | ✅ spawn+cancel | ✅ iteration_budget | — |
| 进程管理 | ✅ process tool | ✅ terminal(bg) | ✅ bash(bg) |
| prompt caching | ✅ prefix stable | ✅ | ✅ |
| Gateway/Desktop | ❌ (product) | ✅ | ❌/✅ |

**结论**：MicroAgent 核心 Agent 能力与 Hermes/Claude Code 持平。缺失的 Gateway/Desktop/Profiles 属于产品层，作为可嵌入库不需要。
