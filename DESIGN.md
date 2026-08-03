# MicroAgent 设计说明

> 版本 1.0.0 | Python ≥3.14 | ~10,400 行核心代码 | 34 内置工具 | 1037 测试

**MicroAgent 是一个将 AI Agent 的核心循环压缩到 10,000 行以内的可嵌入 Python 库——它不做产品，只做引擎。**

**核心机制只有三件事：一个 `while not budget.exhausted` 循环交替驱动 LLM 思考和工具执行，一套 4 层压缩金字塔在上下文溢出时自动做信息分级管理（零开销预处理 → Snip 裁剪 → LLM 结构化摘要 → 熔断），以及一层 Protocol 抽象让 Memory、Skill、Hook、Tool 全部可插拔替换而不改核心代码。**

## 一、定位与设计原则

MicroAgent 是一个**可嵌入的通用 AI Agent 核心库**。它不是产品（不像 Hermes Agent 附带 Gateway/Desktop/Dashboard），而是一个可以嵌入任何 Python 应用的 Agent 运行时。

### 核心原则

1. **最小内核** — 核心循环 LLM→Tool→LLM 是唯一必选路径，所有能力通过 Protocol 扩展
2. **零依赖核心** — 必装仅 6 个（openai、pydantic、anyio、httpx、pyyaml、rich），其余 optional
3. **TDD 全覆盖** — 1037 测试（unit + smoke + e2e + 10 integration），red-green-refactor 驱动
4. **信息分通道** — 借鉴 Claude Code，压缩走 4 层金字塔

### 架构全景

```
┌──────────────────────────────────────────────────────┐
│                 Surface Layer                         │
│  CLI (ANSI boxed)                                 │
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
│ OpenAI   │  │ 34 tools     │  │ SQLite WAL   │
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

registry = ToolRegistry(_default_builtins())   # 34 内置工具
registry.to_openai_tools()                     # → OpenAI function schema
```

### 34 内置工具

| 分类 | 工具 | 说明 |
|------|------|------|
| 文件 | `read_file`, `write_file`, `edit_file`, `grep`, `glob` | 文件 I/O |
| 终端 | `bash`, `process` | 前台 + 后台进程管理 |
| 网络 | `web_search`, `web_fetch`, `context7` | 搜索 + 文档 |
| 浏览器 | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_back`, `browser_scroll`, `browser_press`, `browser_console`, `browser_get_images`, `browser_vision` | Playwright |
| 代码 | `execute_code`, `lsp` | 沙箱执行 + LSP 代码智能 |
| 视觉 | `vision_analyze` | base64 + vision API |
| 会话 | `session_search` | FTS5 历史搜索 |
| 代理 | `task`, `todo`, `plan`, `exit` | 子代理 + 计划 |
| 交互 | `question` | 用户输入（非阻塞） |
| 技能 | `skill_manage`, `skills_list` | create/patch/list/delete |
| Git | `git` | git 操作封装 |
| 文件树 | `file_tree` | 目录结构可视化 |
| MCP | `mcp_connect` | 运行时连接 MCP server |

### 权限引擎 (`core/permission.py` — 190 行)

```python
DEFAULT_RULES = (
    Rule("read_file", {}, Decision.ALLOW),
    Rule("bash", {}, Decision.ASK),
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
get_context_window("oc-d4f") → 1_048_576     # deepseek-v4-flash, models.dev 缓存
_estimate_cost("gpt-4o", 1000, 500) → $0.0075  # 364 模型定价缓存
get_pricing("tx-d4f") → (0.126, 0.252)        # 别名 → deepseek-v4-flash
```

定价数据来自 [models.dev](https://models.dev/api.json)，本地缓存 364 个模型的价格 +
上下文窗口（`llm/pricing.py` + `models_cache.json`）。网关别名（tx-d4f →
deepseek-v4-flash, tx-d4p → deepseek-v4-pro）通过 `_ALIAS_TO_CANONICAL` 映射表解析。
`/models refresh` 命令重新下载最新数据。

**费用显示**：内部以 USD 核算（Budget、models.dev 缓存保持 USD），仅在显示层转 CNY。
`currency.py` 的 `format_cost()` / `format_price_per_1m()` 输出 `¥` 符号，汇率可通过
`MICROAGENT_CURRENCY_RATE` 环境变量调整（默认 7.20）。

---

## 五、会话管理 (`session/`)

### SessionRunner (`runner.py` — 654 行)

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

### 4 层压缩金字塔 (`compress.py` — 697 行)

| 层 | 名称 | API | 触发 | 操作 |
|----|------|-----|------|------|
| L1 | Micro-Compact | 零 | 60% 窗口 | 裁剪工具结果 >500 chars |
| L2 | Snip | 零 | 80% 窗口 | 删除最早 tool_result |
| L3 | LLM 摘要 | 一次 | 窗口-8k | 7 章节结构化摘要 |
| L4 | 熔断 | 零 | 连续 3 次失败 | 300s 冷却 + 占位符 |

**7 章节摘要模板**：请求和意图 | 技术决策 | 文件和代码 | 错误修复 | 所有用户消息 | 待办 | 当前进度

### SQLiteStore (`core/store.py` — 225 行)

```python
store = SQLiteStore("~/.microagent/sessions.db")  # WAL 模式
await store.append(session_id, message)            # JSON 序列化
await store.load_history(session_id)               # → list[Message]
await store.list_sessions()                        # → list[str]
```

### Budget 树形预算 (`session/budget.py` — 169 行)

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

### CLI (`surface/cli.py` — 855 行)

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

MicroAgent 是 **CLI + 核心库**，不提供 Web 或 TUI 界面。集成方如需 Web/TUI，可使用 Python API 自行构建。

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
1036 passed, 1 skipped, 0 failures  —  ~13,030 / 10,419 = 125% test/code ratio（unit+smoke+e2e，82% 行覆盖率）
```

| 测试文件 | 覆盖 |
|---------|------|
| test_runner*.py | SessionRunner, Budget, 中断/溢出/steer/plan 模式 |
| test_builtins.py | 34 工具注册 |
| test_compaction_pyramid.py + test_compress* | 4 层压缩 + 内部函数 |
| test_session_persist.py | 会话持久化 |
| test_process.py | 进程管理（start/poll/wait/write/kill） |
| test_browser*.py | Playwright 工具（mock） |
| test_lsp*.py | LSP 客户端（fake server）+ 辅助函数 |
| test_mcp*.py | MCP client/connect（mock SDK） |
| test_cli*.py | CLI 命令处理 + 渲染 + 辅助函数 |
| test_smoke.py | 导入 + 生命周期 |
| test_e2e.py | 完整 agent turn（工具/预算/持久化） |
| test_config.py / test_permission.py | 配置/权限 |
| test_ssh_terminal.py / test_terminal_backend.py | 终端后端（mock paramiko） |
| test_credential_pool.py / test_memory_extractor.py | LLM 层 |

---

## 十三、已知问题与优化方向

| 优先级 | 问题 | 现状 | 方案 |
|:---:|------|------|------|
| 🔴 | 多级子代理嵌套 | 单级 `task.spawn()`，子代理不能创建孙子代理 | 引入 orchestrator 角色，允许子代理再 spawn |
| 🔴 | 增量压缩 | 每次全量 LLM 摘要，旧摘要被丢弃 | `CompactionState.previous_summary` 迭代更新 |
| 🟡 | 文件附件恢复 | L3 摘要后 Agent 丢失文件上下文 | 压缩时记录最近 N 个文件，自动 re-attach |
| 🟡 | Session 搜索 | LIKE 查询，无 ranking | 升级 FTS5（同 memory provider） |
| 🟡 | 终端多后端 | local/docker/ssh | 按需增加 modal/daytona（非核心） |
| 🟡 | 浏览器 page 类型标注 | `page: object` 导致 Pyright 报错 | `TYPE_CHECKING` 下导入 Playwright Page 类型 |
| 🟢 | 完整插件框架 | 3 Protocol（PreLLM/ToolHook/ContextSource） | 按需扩展（非核心） |
| 🟢 | Skill hub 远程安装 | 仅本地文件 | 增加 `skills install` 命令（非核心） |

---

## 十二、与 Hermes / Claude Code / OpenCode 详细对比

### 12.1 规模对比

| 维度 | MicroAgent | Hermes Agent | Claude Code |
|------|-----------|-------------|-------------|
| 核心代码量 | ~10,400 LOC | ~50,000+ LOC (含 gateway) | 闭源（估计 ~50k+ LOC） |
| 核心循环模块 | 654 行 `runner.py` | 6,055 行 `run_agent.py` | 闭源 |
| 工具数量 | 34 | 69（30+ 为核心工具） | 10+（read/write/bash/grep/glob/edit） |
| 压缩代码量 | 697 行 `compress.py` | 3,342 行 `context_compressor.py` | 闭源（5 层金字塔） |
| CLI 代码量 | 855 行 | 16,304 行 | 闭源（产品级 CLI） |
| 测试数量 | 1037（82% 行覆盖率） | ~17,000 | 闭源 |

### 12.2 核心 Agent 能力逐项对比

#### Agent 循环

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| 流式 LLM 调用 | ✅ `stream()` — TextDelta + ToolCallDelta | ✅ 完整 streaming pipeline | ✅ 流式 |
| 工具并发执行 | ✅ `anyio.create_task_group` | ✅ 异步并发 | ✅ 并发 |
| 思考/推理过程 | ✅ `TextDelta(kind="thinking")` — 流式 reasoning | ✅ `reasoning` 存储 + 显示 | ✅ extended thinking |
| 预算控制 | ✅ 树形 Budget（spawn + cancel_event） | ✅ `iteration_budget` + credential_pool | ✅ token budget |
| 中断/取消 | ✅ `cancel_event` 子树取消 | ✅ `interrupt()` + grace call | ✅ 中断 |
| 回退模型 | ✅ `CredentialPool` 自动切换 key | ✅ `fallback_model` + `credential_pool` | — |

#### 上下文管理

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| 压缩层数 | 4 层金字塔 | 1 层（ContextCompressor） | **5 层金字塔** |
| 零开销预处理 | ✅ Micro-Compact（L1）+ Snip（L2） | ❌ 直接 LLM 摘要 | ✅ Micro-Compact + Snip（L3+L2） |
| 结构化摘要 | ✅ 7 章节（请求/决策/文件/错误/用户消息/待办/进度） | ✅ 9 section（Goal/Progress/Decisions/Resolved/Pending…） | ✅ 9 部分 XML（<analysis> + <summary>） |
| 摘要用同一模型 | ✅ | ❌ 专用 auxiliary 模型（省钱） | ✅ 同一模型（复用 cache） |
| 增量压缩 | ❌ | ✅ iterative summary updates | ✅ iterative |
| 文件附件恢复 | ❌ | ✅ 大文件存磁盘 | ✅ 最近 5 文件 × 5k tokens，总额 50k |
| 熔断机制 | ✅ 连续 3 次 → 300s 冷却 | ✅ 600s 摘要失败冷却 | ✅ Circuit Breaker 连续 3 次 |
| 递归守卫 | ✅ `_is_compaction_call` flag | ✅ 内部标记 | ✅ `querySource` 检查 |
| 触发公式 | `window × 60%` (自适应) | `max(0.5*window, MINIMUM)` | `window - 13,000` (p99 统计) |
| 阈值感知 | ✅ 12 模型窗口表 + 前缀匹配 | ✅ provider metadata + 运行时查询 | ✅ 内置 |
| Lost in the Middle 对策 | ❌ 保留最新 4 条 | ❌ tail 保护 | ✅ 全量重写（不保留最近 N 条） |

**关键差距**：Claude Code 的"全量重写"是最激进也最有效的策略——它不保留最近 N 条消息，而是把所有历史一刀切全送进摘要器。MicroAgent 当前保留最新 4 条，Hermes 按 token 预算保护尾部。

#### 工具系统

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| 文件 I/O | `read_file`, `write_file`, `edit_file`, `grep`, `glob` | ✅ 同名工具 | ✅ 同名工具 |
| 终端 | `bash` + `process`（start/poll/kill/wait/log/write/list） | `terminal`（前景/后台/PTY/多后端） | `bash`（前景/后台） |
| 浏览器 | `browser_navigate/snapshot/click/type/back/scroll/press/console/get_images/vision` (Playwright) | `browser_navigate/click/type/snapshot/vision/scroll…` (Playwright) | ❌ 无原生浏览器 |
| 网络搜索 | `web_search` (DuckDuckGo lite) + `web_fetch` + `context7` | `web_search` (多引擎) + `web_extract` | `web_search` + `web_fetch` |
| 代码执行 | `execute_code` (子进程) | `execute_code` (子进程 + venv) | `bash` (执行代码) |
| LSP 代码智能 | `lsp` (stdio, 5 语言) | ❌ | ❌ |
| 用户交互 | `question` (非阻塞 input) | ❌ | ❌ |
| 视觉 | `vision_analyze` (base64 + vision API) | `vision_analyze` (图片分析) | ✅ 内置 vision |
| 子代理 | `task` (单级 spawn) | `delegate_task` (多级嵌套 + orchestrator) | `task` (多级子代理) |
| 记忆 | `session_search` (FTS5) | `memory_search` (FTS5) | — |
| Skill/插件 | `skill_manage` (create/patch/list/delete) | `skill_manage` + `skills_hub` | CLAUDE.md (配置文件) |
| TODO/计划 | `todo`, `plan`, `exit` | `todo` (agent-level) | ✅ todo |
| **终端多后端** | ✅ LocalTerminal + DockerTerminal (+ SSH) | ✅ local/docker/ssh/modal/daytona/singularity | ❌ 仅 local |
| 权限系统 | ✅ Rule + ScriptRule + Pattern 匹配 | ✅ approval 系统（多级） | ✅ permission 系统 |
| 工具输出大小限制 | ✅ ToolResultDelta(200 chars 摘要) | ✅ truncate + prune | ✅ 大结果存磁盘 |

**关键差距**：
- **Hermes 终端多后端**：local/docker/ssh/modal/daytona/singularity — 6 种执行环境。MicroAgent 有 local + docker + ssh。
- **Hermes delegate_task**：support orchestrator role（可再生成子代理），多级嵌套。MicroAgent 单级。
- **工具注册系统**：MicroAgent 用 Pydantic `@tool` 装饰器（~100 LOC），Hermes 用 `registry.register()` 显式 API（~810 LOC）。

#### 会话与持久化

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| 存储引擎 | SQLite WAL | SQLite WAL + FTS5 | 本地 transcript 文件 |
| 自动保存 | ✅ 每个 turn 自动 append | ✅ `sync_turn()` | ✅ 自动 |
| 多会话 | ✅ `/new` `/list` `/resume` | ✅ sessions + profiles | ❌ 单会话 |
| 会话恢复 | ✅ `SessionRunner.resume(store)` | ✅ `session_start` + 记忆 | ❌ 退出即丢失 |
| Session 搜索 | ✅ `session_search` 工具 (LIKE) | ✅ `session_search` 工具 (FTS5) | — |
| 多 Profile | ❌ (product) | ✅ 多 profile 隔离 | ❌ |

**关键差距**：Hermes 的 `SessionDB` 有 FTS5 全文索引（MicroAgent 用 LIKE），Hermes 支持多 profile 隔离（MicroAgent 不需要）。

#### 记忆系统

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| 存储引擎 | SQLite + FTS5 | 8 个 provider 生态 | — |
| 自动记忆提取 | ✅ MemoryExtractor (LLM, fire-and-forget) | ✅ 通过 memory provider | — |
| 语义搜索 | ✅ `recall(query, k)` | ✅ `prefetch(query)` | — |
| 批量写入 | ✅ `batch_write(memories)` | ✅ | — |
| 支持的 Providers | SQLiteMemoryProvider | honcho, mem0, supermemory, byterover, hindsight, holographic, openviking, retaindb | — |

**关键差距**：Hermes 有 8 个 memory provider 的插件生态（honcho、mem0 等），MicroAgent 目前只有 SQLite 内置。但 MicroAgent 的 MemoryProvider Protocol 设计允许用户自行集成任何 backend。

#### 技能系统

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| 加载器 | ClaudeSkillLoader + Composite（keyword+fuzzy） | scan_skill_commands() + YAML frontmatter | CLAUDE.md 文件 |
| 运行时管理 | `skill_manage` + `skills_list` 工具 (create/patch/list/delete) | `skill_manage` + skill editor | 手动编辑文件 |
| 生命周期 | Curator（active → stale → archived，pinned） | Curator（同上） | 无 |
| 双生态 | skills/ + ~/.hermes/skills/ | skills/ + optional-skills/ + hub | CLAUDE.md 单文件 |
| 与 Memory 集成 | ✅ skill 加载状态持久化 | ✅ | N/A |

**关键差距**：Hermes 的 skill hub 支持远程安装（`hermes skills install official/...`），MicroAgent 只有本地文件。Claude Code 的 CLAUDE.md 更简单——一个 markdown 文件，手动管理。

#### LLM 适配

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| API 协议 | OpenAI Chat Completions (唯一) | OpenAI + Anthropic + Gemini + Codex + Bedrock | Anthropic (原生) |
| Provider 插件 | ❌ 单协议 | ✅ 31 个 model-provider 插件 | ❌ 单一模型 |
| 多模型路由 | ❌ | ✅ smart_model_routing | ❌ |
| 回退模型 | ✅ CredentialPool (同 provider 多 key) | ✅ fallback_model + credential_pool | ❌ |
| 成本计算 | ✅ 11 模型定价表，前缀匹配 | ✅ provider pricing API | ✅ 内置 |
| reasoning_effort | ✅ (low/medium/high) | ✅ | ✅ |
| service_tier | ✅ (auto/default/flex) | ✅ | ✅ |
| 模型 Context Window 感知 | ✅ 12 模型窗口表 | ✅ provider metadata 运行时查询 | ✅ 内置 |

**关键差距**：Hermes 是唯一支持多 provider 协议的（OpenAI + Anthropic + Gemini + Codex + Bedrock），MicroAgent 只支持 OpenAI 兼容协议（但可覆盖 90% 的 API 端点，包括 vLLM、Ollama、DeepSeek 等）。

#### 平台分发

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| CLI | ✅ ANSI boxed 流式 | ✅ Rich + prompt_toolkit | ✅ 产品级 CLI |
| TUI | ✅ textual (optional extra) | ✅ Ink/React TUI（主力界面） | ❌ CLI only |
| Web Dashboard | ✅ FastAPI SSE (optional extra) | ✅ Docusaurus + WebSocket PTY | ❌ |
| Electron Desktop | ❌ | ✅ 独立 Electron App | ✅ 独立 Electron App |
| Gateway（消息平台） | ❌ | ✅ 20+ 平台 (Telegram/Discord/Slack/WhatsApp...) | ❌ |
| Kanban 多 Agent 协作 | ❌ | ✅ SQLite-backed board + worker | ❌ |
| ACP（IDE 集成） | ❌ | ✅ VS Code / Zed / JetBrains | ✅ VS Code 插件 |

**关键差距**：这一层面 MicroAgent 的设计意图就是不覆盖——作为可嵌入库，这些是集成方（使用 MicroAgent 的应用）的责任。Hermes 和 Claude Code 是独立产品，所以自带全平台分发。

#### 扩展性

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| Hook 系统 | ✅ PreLLMHook + ToolHook + ContextSource | ✅ lifecycle hooks + plugin system | ✅ hooks |
| 插件系统 | 🟡 3 Protocol（覆盖 80%） | ✅ 完整 plugin framework | ✅ MCP servers |
| MCP 客户端 | ✅ `connect_mcp_stdio` | ✅ 内置 MCP client | ✅ 内置 MCP client |
| Cron 调度 | ✅ APScheduler (optional extra) | ✅ 内置 cron system | — |

**关键差距**：Hermes 的 `PluginManager` 是完整的插件发现/注册/生命周期系统，支持 `pre_tool_call` / `post_tool_call` / `pre_llm_call` / `post_llm_call` / `on_session_start` / `on_session_end` 6 个 hook。MicroAgent 的 3 个 Protocol 更轻量但覆盖更少。

#### 代码质量

| 特性 | MicroAgent | Hermes | Claude Code |
|------|-----------|--------|-------------|
| Python 版本 | 3.14+ | 3.11+ | N/A (Node.js) |
| 类型系统 | ✅ 完整类型标注 + `slots=True` + `frozen=True` | ✅ 大规模类型系统 | ✅ TypeScript |
| 测试策略 | ✅ TDD, 1037 测试（unit+smoke+e2e+integration） | ✅ ~17k 测试 + CI parity | N/A |
| 测试隔离 | ✅ 子进程 per test file | ✅ 子进程 per test file | N/A |
| 构建系统 | hatchling | setuptools + uv | npm/esbuild |
| 依赖管理 | `>=floor,<next_major` 上限 | 同上 + SHA pinning | npm lock |

---

### 12.3 核心差距总结

| 优先级 | 差距项 | MicroAgent 现状 | 对标 | 差异说明 |
|--------|--------|---------------|------|---------|
| 🔴 高 | 多级子代理嵌套 | 单级 `task.spawn()` | Hermes orchestrator 角色 | 不支持子代理创建孙子代理 |
| 🔴 高 | 增量压缩 | 每次全量 LLM 摘要 | Hermes iterative summary | 已有摘要被丢弃，浪费 token |
| 🟡 中 | 文件附件恢复 | L3 摘要后无文件恢复 | Claude Code 最近 5 文件 | 压缩后 Agent 丢失文件上下文 |
| 🟡 中 | 终端多后端 | local + docker + ssh | Hermes 6 种 | 缺 modal/daytona/singularity |
| 🟡 中 | Session 搜索 | LIKE 查询 | Hermes FTS5 | LIKE 性能差，不支持 ranking |
| 🟡 中 | 多 Provider 协议 | 仅 OpenAI 兼容 | Hermes 31 providers | 覆盖 ~90% 端点，但缺 Anthropic/Gemini 原生 |
| 🟡 中 | 浏览器工具 | 10 个（含 back/scroll/press/console/get_images/vision） | Hermes 10 个 | 已对齐 |
| 🟡 中 | LSP 代码智能 | 5 语言（py/ts/rs/go/cpp） | OpenCode 完整 LSP | 已对齐（真实 stdio） |
| 🟢 低 | 完整插件框架 | 3 Protocol | Hermes PluginManager | Protocol 覆盖 80% 场景 |
| 🟢 低 | Skill hub 远程安装 | 仅本地文件 | Hermes `skills install official/...` | 用户需手动放置 skill 文件 |
| 🟢 低 | MCP 运行时连接 | `mcp_connect` 工具 | Hermes 内置 MCP client | 已对齐 |
| ⚪ N/A | Gateway 20+ 平台 | — | Hermes | 产品层，MicroAgent 不做 |
| ⚪ N/A | Electron Desktop | — | Hermes / Claude Code | 产品层，MicroAgent 不做 |
| ⚪ N/A | Kanban 多 Agent | — | Hermes | 产品层，MicroAgent 不做 |
| ⚪ N/A | 多 Profile 隔离 | — | Hermes | 产品层，MicroAgent 不做 |
| ⚪ N/A | ACP IDE 集成 | — | Hermes | 产品层，MicroAgent 不做 |

### 12.4 定位差异

```
MicroAgent         — 可嵌入库 (~5k LOC)，给 Python 应用提供 Agent 能力
Hermes Agent       — 独立产品 (~100k LOC)，全平台全客户端覆盖
Claude Code        — 闭源产品 (~50k+ LOC)，Anthropic 官方 AI 编程工具
OpenCode           — 开源 CLI (~10k LOC)，专注编程场景的 Agent
```

MicroAgent 的设计哲学是**最小可用内核 + 可插拔扩展**。~10,400 行代码覆盖了 Agent 循环的每个关键环节——从 LLM 调用到工具执行，从会话持久化到上下文压缩——但把 Gateway/Desktop/Profiles/Kanban 留给集成方。这与 Hermes 的"全家桶"和 Claude Code 的"闭源精品"是不同的路线。
