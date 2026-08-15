# MicroAgent API Guide

快速上手指南：安装、配置、使用 MicroAgent。

## 安装

```bash
pip install microagent
```

## 5 分钟快速开始

### 1. 配置

```bash
# 创建配置文件
mkdir -p ~/.microagent
cat > ~/.microagent/config.yaml << 'EOF'
model:
  base_url: "https://api.openai.com/v1"
  api_key: "sk-your-api-key"
  model: "gpt-4o"
system_prompt: "你是一个有用的助手。"
EOF
```

### 2. 运行

```bash
# One-shot 模式
microagent "2+2等于几？"

# 交互模式
microagent
>>> 你好
>>> /new         # 新会话
>>> /list        # 列历史会话
>>> /resume      # 恢复上次会话
>>> /compact     # 手动压缩上下文
>>> /help        # 查看命令
```

### 3. Python API

```python
from microagent import Agent, Config, Message

# 从配置文件加载
cfg = Config.from_file()
agent = Agent.from_config(cfg.llm, system_prompt=cfg.system_prompt)

# 同步调用
response = agent.run("用一句话解释什么是 GIL")

# 异步调用
response = await agent.arun([Message.user("Python 列表和元组的区别？")])
```

## 配置方式

优先级：CLI 参数 > 环境变量 > 配置文件 > 默认值

```bash
# CLI 参数
microagent --model gpt-4o --base-url https://api.openai.com/v1 "hello"

# 环境变量
MICROAGENT_MODEL=gpt-4o MICROAGENT_API_KEY=sk-xxx microagent "hello"
```

## Python API 详解

### Agent — 核心门面

```python
from microagent import Agent, LLMConfig, Message, SQLiteStore

# 基础用法
agent = Agent.from_config(
    LLMConfig(base_url="...", api_key="...", model="gpt-4o"),
    system_prompt="You are a helpful assistant.",
    max_iterations=25,           # 最大工具调用轮次
)

# 带会话持久化
store = SQLiteStore("~/.microagent/sessions.db")
agent = Agent.from_config(
    config,
    store=store,
    session_id="my-session",
)

# 简单调用
response = agent.run("你好")       # str → 自动包装为 Message
response = await agent.arun([Message.user("你好")])  # 异步
```

### Message — 对话消息

```python
from microagent import Message

# 用户消息
Message.user("帮我读一下 config.py")

# 助手消息（通常由 Agent 自动生成）
Message.assistant("好的，让我来读取文件。")

# 工具结果
Message.tool_result(
    ToolResult.ok("import os\n\nAPI_KEY = os.environ.get('KEY')"),
    tool_call_id="call_123",
)
```

### SessionRunner — 核心循环

```python
from microagent import SessionRunner, ToolRegistry, LLMConfig, OpenAIChatClient, Budget

runner = SessionRunner(
    llm=OpenAIChatClient(LLMConfig(...)),
    registry=ToolRegistry([...]),
    budget=Budget(max_iterations=25, max_tokens=200_000, max_cost_usd=5.0),
    store=SQLiteStore("sessions.db"),
    session_id="chat-1",
)

messages = [Message.user("hello")]
async for event in runner.run_turn(messages):
    if isinstance(event, TextDelta):
        print(event.text, end="", flush=True)
    elif isinstance(event, ToolCallDelta):
        print(f"\n🔧 {event.name}({event.arguments})")
    elif isinstance(event, TurnComplete):
        print(f"\n✅ Done")
    elif isinstance(event, TurnFailed):
        print(f"\n❌ {event.reason} (code={event.code})")
        # code ∈ interrupted|budget|overflow|llm_timeout|llm_error|compaction|error
        # 按 code 程序化分支，不要匹配 reason 文本
```

## 会话管理

```python
# 创建持久化会话
store = SQLiteStore("~/.microagent/sessions.db")
agent = Agent.from_config(config, store=store, session_id="project-debug")

# 对话...
agent.run("分析这个 bug")
agent.run("修复它")

# 列出所有会话
sessions = await store.list_sessions()

# 恢复历史会话
history = await store.load_history("project-debug")
agent2 = Agent.from_config(config, store=store, session_id="project-debug")
response = await agent2.arun(list(history) + [Message.user("继续之前的工作")])
```

## 记忆

```python
# CLI 默认开启（Hermes 对齐）；库默认关闭——嵌入方必须显式 opt-in，
# 避免库构造在未预期位置创建 ~/.microagent/memory.db：
agent = Agent.from_config(config, memory=True)          # SQLiteMemoryProvider(~/.microagent/memory.db)
                                                        # + LLM 提取器（每轮后台抽取）
                                                        # + 每轮 recall 注入上下文
agent = Agent.from_config(config)                       # 库默认关闭（MICROAGENT_MEMORY=1 可环境开启）
agent = Agent.from_config(config, memory=False)         # 明确关闭
agent = Agent.from_config(config, memory=custom_provider)  # 自定义后端

# write_approval 闸门（Hermes write_approval 语义，默认 False 直写）
provider = SQLiteMemoryProvider("mem.db")
provider.write_approval = True
pending = await provider.pending_memories()
await provider.approve_memory(pending[0].id)
await provider.reject_memory(pending[1].id)
# CLI: /memory [pending|approve <id>|reject <id>]
```

## 技能学习（/learn）

```python
# Hermes 对齐：技能沉淀是刻意行为，不是自动后台循环
result = await agent.learn("我们从对话中总结的流程", kind="chat")
result = await agent.learn("./src/my_tool", kind="dir")
result = await agent.learn("https://example.com/guide", kind="url")
# CLI: /learn chat . 从当前会话学习
# 写入 ~/.microagent/skills（provenance: agent）→ curator 管理生命周期
```

## 自定义工具

```python
from microagent.core.tool import tool
from microagent.core.types import ToolResult
from typing import Annotated
from pydantic import Field

@tool("calculate", description="执行数学计算")
async def calculate(
    expression: Annotated[str, Field(description="数学表达式，如 '2+2*3'")]
) -> ToolResult:
    try:
        result = eval(expression, {"__builtins__": {}}, {})
        return ToolResult.ok(str(result))
    except Exception as e:
        return ToolResult.error(f"计算失败: {e}")

# 共享 per-session 状态的工具声明 exclusive=True：
# runner 会经组级锁串行它们（浏览器 page / LSP server 等）
@tool("my_browser_op", description="操作共享页面", exclusive=True)
async def my_browser_op(ref: str) -> ToolResult: ...

# 注册工具
from microagent.core.tool import ToolRegistry
registry = ToolRegistry([calculate])
```

工具约定：
- schema 描述**不得**引用其他工具名（模型会幻觉调用不存在的工具）
- 每轮工具执行全局并发上限 **10**（`_run_tool_calls` 内 Semaphore）
- LLM 流默认 **300s 空闲看门狗**（`runner.llm_stream_idle_timeout`，0 禁用）

## LLM 失败分类

```python
from microagent import LLMFailure, RETRYABLE_CODES, IdleTimeoutError
from microagent.llm.errors import classify_exception

# TurnFailed.code 是给事件消费者的稳定分类；嵌入方需要更细控制时
# 用 classify_exception 归一化底层异常：
try:
    ...
except Exception as e:
    failure = classify_exception(e)
    if failure.code in RETRYABLE_CODES:
        # timeout | rate_limit | overloaded | server_error | network_error | empty_response
        retry()
    # 非可重试:auth_error | bad_request | context_exceeded | aborted | unknown
```

## 权限控制

```python
from microagent.core.permission import PermissionEngine, Rule, Decision, ScriptRule

rules = (
    Rule("read_file", {}, Decision.ALLOW),
    Rule("bash", {}, Decision.ASK),                     # 需要确认
    Rule("bash", {"command": "ls *"}, Decision.ALLOW),  # ls 命令允许
    ScriptRule("write_file", {}, "./approve_write.sh"), # 外部脚本决策
)
engine = PermissionEngine(rules=rules)
```

## 扩展点（Hooks）

```python
# LLM 调用前修改 system prompt
class MyPreLLMHook:
    async def __call__(self, system_prompt: str) -> str:
        return system_prompt + "\n请用中文回答。"

# 工具调用前后拦截
class AuditHook:
    async def before(self, call, ctx):
        print(f"即将执行: {call.name}")
        return call

    async def after(self, call, result, ctx):
        print(f"执行完毕: {call.name} → {result.content[:50]}")
        return result

# 注入额外上下文
class GitContext:
    async def contribute(self, ctx):
        return f"\n当前分支: main\n最近提交: {subprocess.check_output(['git', 'log', '-1', '--oneline']).decode()}"

agent = Agent.from_config(config)
runner = SessionRunner(
    llm=..., registry=...,
    pre_llm_hooks=(MyPreLLMHook(),),
    tool_hooks=(AuditHook(),),
    context_sources=(GitContext(),),
)
```

## Extras

```bash
pip install microagent[dev]       # pytest, pytest-asyncio
pip install microagent[mcp]       # MCP 客户端支持
pip install microagent[cron]      # 定时任务调度
pip install microagent[ssh]       # SSH 终端后端
pip install microagent[browser]   # Playwright 浏览器自动化
```

## 下一步

- `DESIGN.md` — 完整架构文档（模块详解、数据流图、对比分析、已知问题与优化方向）
- `README.md` — 项目概览
