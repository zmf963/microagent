# MicroAgent

一个 Python 实现的、可嵌入应用的通用 AI 智能体核心库。

## Language

**Turn**:
一次完整的 LLM 对话循环——从用户输入到最终文本响应（可能包含多轮工具调用）。
_Avoid_: conversation, round, exchange

**Iteration**:
Turn 内部的一次 LLM 调用 + 工具执行循环。一个 Turn 包含 1~N 个 Iteration。
_Avoid_: step, loop

**Budget**:
Turn 或 Subagent 的资源预算（iterations / tokens / cost_usd），树形结构，子代理消耗累积上报祖先。
_Avoid_: limit, quota, cap

**Surface**:
用户与 Agent 交互的入口（CLI / TUI / Web / 嵌入 SDK）。
_Avoid_: frontend, interface, client

**Skill**:
可复用的过程性知识，以 SKILL.md 格式存储，按需匹配并注入 system prompt。
_Avoid_: plugin, module, template, playbook

**ContextSource**:
独立扩展点——往 system prompt 注入动态内容（如 git 状态、LSP 符号信息）。
_Avoid_: context provider, system injector

**Subagent**:
通过 `task` 工具派生的子 Agent，拥有独立 session、受限工具集、子预算。父代理只见结果摘要。
_Avoid_: child agent, worker, delegate

**MaterializedTool**:
权限已解析的工具视图（Tool + Decision），传给 LLM 的 tool schema 列表中的一项。
_Avoid_: resolved tool, filtered tool

## Hook Types

**PreLLMHook**:
可改写 LLM 输入的扩展点——在每次 LLM 调用前执行，可修改 TurnContext。
_Avoid_: pre_llm_call, transform hook, interceptor

**ToolHook**:
工具调用前后拦截的扩展点——`before()` 可拒绝或改写参数，`after()` 可改写结果。
_Avoid_: tool interceptor, guard, middleware

**EventBus**:
仅观测的 pub/sub 事件总线——注册 `on()`、发射 `emit()`，异常吞掉不阻断主流程。
_Avoid_: PluginBus, hook registry, event dispatcher
