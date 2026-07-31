# MicroAgent Bug 记录

> 本文件记录对 MicroAgent 逐功能实际测试中发现的问题。
> 生成时间：2026-07-31
> 状态图例：✅ 已修复 | 🟡 待处理 | 🔴 严重

---

## 一、代码缺陷

### 1.1 [✅ 已修复] `SubagentSpec.tools_blocked` 是必填参数
- **文件**：`src/microagent/subagent/manager.py`
- **现象**：`SubagentSpec(name="x", description="d", system_prompt="s", tools_allowed=("read_file",))` 直接报错：
  ```
  TypeError: SubagentSpec.__init__() missing 1 required positional argument: 'tools_blocked'
  ```
- **影响**：README.md 的自定义子代理示例（不传 `tools_blocked`）按文档写会崩溃。
- **修复**：给 `tools_blocked` 加默认值 `()`（空元组 = 不屏蔽任何工具）。
- **验证**：README 示例现可正常运行。

### 1.2 [✅ 已修复] `SteerEvent` 未从顶层包导出
- **文件**：`src/microagent/__init__.py`
- **现象**：`SteerEvent` 在 `core/types.py` 中定义，且是 `Event` 联合类型成员（`run_turn()` 会 yield），但未加入 `__init__.py` 的 `__all__`。用户无法 `from microagent import SteerEvent`。
- **影响**：处理 steer 事件的用户代码无法从顶层导入（需 `from microagent.core.types import SteerEvent`），与其他事件类型（TextDelta/TurnComplete 等均顶层导出）不一致。
- **修复**：加入 `__init__.py` 导入 + `__all__`。公共 API 符号 62 → 63。

### 1.3 [🟡 待处理] `SQLiteMemoryProvider._insert` 无显式 commit
- **文件**：`src/microagent/memory/provider.py`
- **现象**：`batch_write()` 的 `_insert()` 只执行 INSERT，无 `commit()`。数据仅在 sqlite3 连接关闭时自动提交（`close()` 触发）。
- **影响**：**长驻服务**（长时间不 close）中，记忆写入停留在未提交事务。同一连接内可见，但跨进程/异常退出时可能丢失。
- **验证**：单次 `batch_write → close → 新连接` 数据持久化正常（close 时提交）。
- **建议**：在 `_insert` 或 `batch_write` 后加 `self._conn.commit()`。

---

## 二、文档与源码不符

### 2.1 [✅ 已修复] `Decision.ASK_USER` 不存在，源码是 `Decision.ASK`
- **文件**：`API.md`、`DESIGN.md`
- **现象**：文档权限示例用 `Decision.ASK_USER`，但源码 `Decision` 枚举只有 `ALLOW`/`DENY`/`ASK`。
- **影响**：按文档写 `Rule("bash", {}, Decision.ASK_USER)` 报 `AttributeError`。
- **修复**：文档改为 `Decision.ASK`。

### 2.2 [✅ 已修复] `PermissionEngine(*rules)` 写法错误
- **文件**：`API.md`
- **现象**：文档写 `engine = PermissionEngine(*rules)`。源码签名是 `PermissionEngine(rules=rules)`。`*rules` 会把每个 `Rule` 当作位置参数传给 `rules` 形参，实际行为不符预期。
- **修复**：文档改为 `PermissionEngine(rules=rules)`。

### 2.3 [✅ 已修复] 工具名 `plan` 应为 `task_plan`
- **文件**：`README.md`、`DESIGN.md`
- **现象**：文档列工具 `plan`，实际 `@tool` 注册名是 `task_plan`（`todo_plan_exit.py` 中函数名 `plan` 但注册为 `task_plan`）。
- **影响**：用户按文档调 `plan` 工具会得到 "unknown tool: plan"。
- **修复**：文档统一改为 `task_plan`。

### 2.4 [✅ 已修复] 过时统计数字（行数/测试数/依赖数）
- **文件**：`README.md`、`AGENTS.md`、`DESIGN.md`、`pyproject.toml`
- **现象**：多处声称 "7,000 行 / 409 测试 / 340 行 runner / 369 行 CLI / 528 行 compress / 22 工具 / 5 依赖"。
- **实际**：源码 **9,355 行**、**432 测试**、runner **654 行**、CLI **855 行**、compress **697 行**、**34 工具**、核心依赖 **6 个**（含 rich）。
- **修复**：全部文档更新为实际值。公共 API 符号 62 → 63（含 SteerEvent）。

---

## 三、测试用例（非源码 bug，但值得注意）

### 3.1 [✅ 正常] FTS5 多词查询是 AND 语义
- **现象**：`SQLiteMemoryProvider.recall("Python project", k=5)` 返回 0，但 `recall("Python")` 返回命中。
- **解释**：FTS5 的 `MATCH "Python project"` 要求单条记忆**同时**含两个词。若记忆分散在不同条目，则无命中。这是 FTS5 标准行为，非缺陷。

### 3.2 [✅ 正常] `glob` 工具只支持相对路径
- **现象**：`glob(pattern="/tmp/*.txt")` 报 `NotImplementedError: Non-relative patterns are unsupported`。
- **解释**：glob 工具设计为相对路径（工作目录内），绝对路径不支持。属预期行为，文档应注明。

---

## 四、性能/健壮性观察

### 4.1 [🟡 观察] `_cjk_aware_ratio` 中文匹配对长描述仍有限
- **文件**：`src/microagent/skill/loader.py`
- **说明**：已从 Jaccard 修复为 query-coverage + LCS（见 git history），措辞接近的中文查询命中率从 ~0 提升到 10/13。但查询与描述用词差异大时（如 "帮我调试这个 bug" vs "用于难解 bug 的诊断循环"）仍可能不命中。
- **建议**：若需更强中文技能匹配，可加 `triggers`/`when_to_use` 关键词字段，或接入嵌入模型语义检索。

---

## 附：测试方法

所有功能通过 `tests/unit/fake_llm.py` 的 `FakeLLMClient` 驱动实际代码路径验证（无网络依赖），覆盖：
- Agent/SessionRunner/Message/工具循环/流式/预算（第 1 批）
- 权限/自定义工具/会话持久化/技能加载匹配（第 2 批）
- 记忆/压缩/子代理/CLI 命令/终端后端（第 3 批）
- 子代理修复/MCP/cron/steer/plan-build（第 4 批）
- 工具清单核对/压缩完整流程/EventBus/Config（第 5 批）
- 文件工具/bash 实测（第 6 批）

验证结果：`make test` → **432 passed, 1 skipped**。
