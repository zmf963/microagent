---
kind: error_handling
name: 错误处理体系：结构化事件与 ToolResult 统一错误模型
category: error_handling
scope:
    - '**'
source_files:
    - src/microagent/core/types.py
    - src/microagent/session/budget.py
    - src/microagent/core/tool.py
    - src/microagent/session/runner.py
    - src/microagent/core/event.py
    - tests/unit/test_runner_errors.py
---

## 1. 采用的错误处理系统

MicroAgent 采用**基于数据类的事件流 + 结构化结果对象**的错误处理模式，而非传统的异常传播。核心思想是：
- 工具执行失败不抛出异常，而是返回 `ToolResult.error(...)` 
- 会话轮次结束通过 `TurnComplete` / `TurnFailed` 两个互斥事件表达成功或失败
- 预算超支等控制性错误使用专用异常 `BudgetExceeded` 进行快速中断
- 观察者/副作用模块的异常被静默吞掉，保证主循环鲁棒性

## 2. 关键文件与类型定义

- **`src/microagent/core/types.py`**：定义 `ToolResult`（ok/error/denied）、`TurnComplete`、`TurnFailed`、`Event` 联合类型等核心数据结构
- **`src/microagent/session/budget.py`**：定义 `BudgetExceeded` 异常及树形预算跟踪
- **`src/microagent/core/tool.py`**：`FunctionTool.execute_stream` 中捕获所有异常并转为 `ToolResult.error`
- **`src/microagent/session/runner.py`**：会话运行器，统一消费事件流，处理预算耗尽、LLM 截断、工具错误等路径
- **`src/microagent/core/event.py`**：`EventBus.emit` 中 `except Exception: pass` 确保观察者失败不影响主循环

## 3. 架构与约定

### 3.1 工具层错误归一化
所有工具通过 `@tool` 装饰器注册，`FunctionTool.execute_stream` 在 `try/except Exception` 中捕获任意异常，统一包装为 `ToolResult.error(f"{name} failed: {e!r}")`。这意味着工具实现者无需关心错误处理，只需正常返回即可。

### 3.2 会话层事件驱动
`SessionRunner.run_turn()` 是一个异步生成器，持续产出 `Event` 联合类型的实例：
- 成功路径：`TextDelta` → `ToolCallDelta` → `ToolProgressDelta` → `ToolResultDelta` → `TurnComplete`
- 失败路径：任何阶段都可能产出 `TurnFailed(reason=...)` 并终止轮次

### 3.3 预算控制的异常中断
`Budget.consume()` 在超过限制时抛出 `BudgetExceeded` 异常，`SessionRunner` 在各关键点捕获并转换为 `TurnFailed("budget exhausted: ...")`。该异常设计用于快速短路整个调用链。

### 3.4 观察者隔离
`EventBus.emit` 对每个回调单独 try/except，确保订阅者异常不会污染主流程。这是典型的「观察者只读」安全模式。

## 4. 开发者应遵循的规则

1. **工具实现**：不要手动 raise 业务异常，直接返回 `ToolResult.ok(...)` 或 `ToolResult.error(...)`；框架会自动将未捕获异常转为 error
2. **会话消费者**：通过 `isinstance(event, TurnFailed)` 判断失败，而不是依赖异常传播
3. **预算控制**：使用 `BudgetExceeded` 作为唯一的中断信号，不要在业务逻辑中随意抛出自定义异常
4. **扩展点**：新增的 hook/observer 必须保证自身异常不会泄漏到主循环（参考 EventBus 的实现）
5. **测试策略**：参考 `tests/unit/test_runner_errors.py`，通过 FakeLLMClient 模拟各种失败路径验证健壮性

## 5. 与传统异常模式的对比

| 场景 | 传统方式 | MicroAgent 方式 |
|------|----------|----------------|
| 工具执行失败 | raise CustomError | return ToolResult.error() |
| 轮次结束 | 返回值或异常 | yield TurnComplete/TurnFailed |
| 资源超限 | 全局状态检查 | BudgetExceeded 异常快速中断 |
| 监听器崩溃 | 可能中断主循环 | 异常被吞掉，主循环继续 |

这种设计使错误处理显式化、可组合、可测试，特别适合 LLM 驱动的异步多步骤工作流。