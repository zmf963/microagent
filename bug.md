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

### 1.3 [✅ 已修复] `SQLiteMemoryProvider._insert` 无显式 commit
- **文件**：`src/microagent/memory/provider.py`
- **现象**：`batch_write()` 的 `_insert()` 只执行 INSERT，无 `commit()`。数据仅在 sqlite3 连接关闭时自动提交（`close()` 触发）。
- **影响**：**长驻服务**（长时间不 close）中，记忆写入停留在未提交事务。同一连接内可见，但跨进程/异常退出时可能丢失。
- **验证**：2026-08-03 复查源码，`_insert` 与 `batch_write` 均已带 `self._conn.commit()`（provider.py:211/241）。已修复。

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

---

## 五、2026-08-03 修复轮（第三轮疯狂测试 → C1–C6 修复）

> 该轮发现 15 项（8🔴 5🟡 2🔵），均以 FakeLLMClient 驱动真实 SessionRunner 验证。
> 以下 6 项 🔴 已修复（每项一个 commit，全程 `pytest tests/ -q` 绿）：

| # | 问题 | 修复 commit |
|---|------|------------|
| 2.5 | thinking deltas 混入 `content_parts`：推理文本被持久化进 assistant 消息；thinking-only + length 被误判为截断 | `e300238` |
| 2.4 | partial tool_calls + `stop_reason=length` 走"正常执行"分支：参数 JSON 必不完整，白烧 4+ LLM 调用 | `2a92a3f` |
| 2.1 | plan 模式只过滤广告给 LLM 的工具清单，执行层无拦截——write_file/git commit 在 plan 模式照样执行 | `d2a7547`（执行层硬拦截 + bash 只读白名单启发式） |
| 2.3 | 硬取消（task.cancel）留孤儿 tool_call，OpenAI API 拒绝恢复会话 | `9e4ca8b` |
| 2.2 | interrupt 非抢占：`sleep 60` 工具期间忽略中断 | `36cfa06`（watcher 取消 task group + execute_code BaseException 杀进程） |
| 2.8 | `arguments={"_raw": ...}`（LLM JSON 损坏兜底）使工具执行抛 TypeError | `88415aa` |

> 修正记录：初判 2.7「snip 无效 + 无限空转」中"无限空转/死循环"**不成立**——重读
> `compress.py` 256-276 行，索引 i 单调递增，循环必然退出；实际行为是"全保护时静默
> 返回原样 + O(n²) 白扫"，严重度 🔴→🟡。3.5（死循环风险）同因关闭。其余 🟡/🔵 项
> （2.6/2.7/3.1/3.2/3.3/3.4/4.1/4.2）在后续 commit 中处理。

---

## 六、2026-08-03 第四轮疯狂测试（边界/错误路径 · 探针 A-D）

> 方法：临时探针脚本（/tmp）直接调用真实工具函数与真实 SessionRunner，聚焦边界条件。
> 基线：pytest `tests/ -q --ignore=tests/benchmark` → 1036 passed, 11 skipped（修复后 1042 passed）。

### 🔴 严重

**6.1 LLM API 异常逃逸 `run_turn` / `Agent.arun`，不会变成 TurnFailed** ✅ **已修复 (a25a895)**
- **修复**：runner 流循环包 try/except；未产生任何用户可见输出时重试一次（走外层 turn 循环），
  重试仍失败或已有部分内容流出 → `TurnFailed("LLM error: ...")`，Agent.arun 返回 `[error: ...]`。
  CancelledError 不捕获，interrupt 语义不变。测试：TestLLMStreamErrors 4 个（test_runner_errors.py）。
- **文件**：`src/microagent/llm/client.py`（stream `raise`）+ `src/microagent/session/runner.py:546`
- **现象**：`_create_with_backoff` 对非重试错误（400、无重试的 401/403、退避耗尽）直接 `raise`；
  runner 的 `async for event in self.llm.stream(...)` **无 try/except 包裹**，异常一路冒出 run_turn
  和 Agent.arun → 调用方收到裸异常（探针实测 `RuntimeError("boom")` 逃逸）。
- **影响**：LLM API 网络故障/限额/坏请求 → 整个对话调用崩溃，而不是可恢复的 `TurnFailed`。
  真实场景：网关超时、key 失效、模型不存在，全部表现为未捕获异常。
- **验证**：ExplodingLLM 驱动 run_turn → 异常直接冒出（探针 C-1）。

### 🟡 应修复

**6.2 `edit_file` 二进制文件抛 UnicodeDecodeError 逃逸** ✅ **已修复 (476933e)**
- **修复**:UnicodeDecodeError → ToolResult.error('binary file');新增 50MB stat 前置上限。测试 2 个(test_tool_fixes.py)。
- **文件**：`src/microagent/tools/builtins/edit_file.py`
- **现象**：`p.read_text()` 对二进制文件抛 `UnicodeDecodeError`，无 try/except（FunctionTool.execute
  只兜 TypeError）。runner `_settle` 的泛化 except 会兜成错误结果，但直连 execute 崩溃、且错误信息不友好。
- **附带**：`edit_file` 无文件大小上限（read_file 50MB / grep 10MB 均有保护），大文件整读 OOM 风险。

**6.3 `vision_analyze` 无大小限制 + 目录路径抛 IsADirectoryError** ✅ **已修复 (ce735d8)**
- **修复**:目录 → error('not a file');新增 20MB 原始字节上限(base64 前拦截)。测试 2 个(test_vision.py)。
- **文件**：`src/microagent/tools/builtins/vision_analyze.py`
- **现象**：100MB 图片被 base64 成 ~139MB 塞进 ToolResult.content（探针验证 len=139,810,201）
  → ToolOutputStore 落盘 + token 爆炸；目录路径 `_encode_image` 的 `p.read_bytes()` 抛
  `IsADirectoryError` 逃逸。read_file/web_fetch 均有大小保护，此工具没有。

**6.4 `SQLiteStore.load_history` 对损坏数据行抛 JSONDecodeError** ✅ **已修复 (d8d90ee)**
- **修复**：`_load` 改为逐行 try/except 跳过坏行（与 session_summaries 同模式）；全坏返回 []。
  测试：TestSQLiteStore 新增 2 个（中间行损坏跳过 / 全坏返回空）。
- **文件**：`src/microagent/core/store.py`
- **现象**：单行 data JSON 损坏（手改库/中断写入）→ `load_history` 整库抛 `JSONDecodeError`，
  该 session 无法加载（resume 崩溃）。对比：`session_summaries` 有每行 try/except，load_history 没有。

**6.5 `SQLiteMemoryProvider.recall("")` 返回全部记忆** ✅ **已修复 (96d4fba)**
- **修复**:recall 入口空白查询直接返回 ()。测试 1 个(test_memory_provider.py)。
- **文件**：`src/microagent/memory/provider.py`
- **现象**：空查询 `MATCH ''` 在 FTS5 中匹配所有行 → 泄漏全部记忆进上下文（探针 D 验证）。
  调用方用空/空白查询时发出所有内容。

### 🔵 可选

**6.6 `write_file backup` 静默覆盖已有 `.bak`** ✅ **已修复 (5e77301, 3f97e2d)**（文件 write_file.py）：
第二次 backup 覆盖旧备份，无提示 → 修复：结果消息追加 '(overwrote existing backup)'。**`git` 白名单允许 `--amend`**（git.py）：
`-m 'x' --amend` 可通过，本地重写历史的语义 → 修复：按子命令禁 flag(commit --amend、branch -d/-D/--delete)。**`bash` >100KB 输出截断标记的
"[truncated: N bytes beyond]" 统计约为 0**（收集阶段已截断，数字失真——纯修剪指标）→ 已加注释说明语义。

**6.7 `Budget.spawn()` 父耗尽时产出 max_iterations=0 子预算** ✅ **已文档化 (3f97e2d)**（budget.py）：
`min(max(1, rem//3), rem)` 在 rem=0 时为 0 → 子代理立即 BudgetExceeded（死预算）。
父预算耗尽时这是合理的"没得给"，但值得文档标注。

---

## 七、第四轮疯狂测试（Round 5,探针 E–H)— 2026-08-04

> 方法：探针 E(web_fetch SSRF)、F(grep SIGALRM + glob/grep 越界）、G(cron 调度器）、H（并发 run_turn 同 session)。
> 基线：1050 passed, 11 skipped。

### 🔴 严重

**7.1 cron 输出落盘存在路径穿越 — job.name 未消毒** ✅ **已修复 (4d01e96)**
- **修复**:`_save_cron_output` 消毒为单一路径分量；`add_job` 拒绝含路径分隔符的名字。测试 3 个。
- **文件**：`src/microagent/cron/scheduler.py:85`(`_save_cron_output`)
- **现象**:`out_dir = base_dir / "output" / job_id`,job.name 含 `../../escaped` 时
  直接写到 base_dir 之外（探针 G-1 验证：文件落到了 base 外）。
- **影响**：任意路径写入（内容部分可控——prompt/response 包在 markdown 模板里，但路径完全可控）。
  攻击面：能注册 cron job 的调用方（gateway 配置/工具）。

**7.2 web_fetch SSRF 未封 CGNAT 100.64.0.0/10(Tailscale 网段）** ✅ **已修复 (00f6a33)**
- **修复**:`_BLOCKED_RANGES` 增加 100.64.0.0/10 + 198.18.0.0/15。测试 3 个。
- **文件**：`src/microagent/tools/builtins/web_fetch.py`(`_BLOCKED_RANGES`)
- **现象**：探针 E-1:`100.64.1.1` / `100.115.92.2` 均不拦截。Tailscale tailnet 内网服务
  （恰好常用 100.64/10）完全可达；`198.18.0.0/15`(benchmark，部分运营商/设备用）同样未封。
- **影响**：LLM 被诱导 fetch tailnet 内网服务（路由器、NAS、内网 API)→ SSRF。
  对比：`.local` 主机名、`169.254.169.254`(云 metadata)、十进制/十六进制 IP 变形都已正确封堵。

### 🟡 应修复

**7.3 grep 正则超时静默返回"无匹配"— 假阴性无披露** ✅ **已修复 (fbb2701)**
- **修复**:`_search_with_alarm` 超时 raise,grep 计数并在结果尾部追加 `[N line(s) skipped: regex timeout]`。测试 1 个。
- **文件**：`src/microagent/tools/builtins/grep.py`(`_search_with_alarm` 超时返回 None)
- **现象**：灾难性回溯正则（如 `(a+)+$`）在某行触发 5s SIGALRM 超时后，该行被当作
  "不匹配"静默跳过；全部行超时时 LLM 看到 `(no matches)` —— 但实际可能有匹配。
- **影响**：正确性问题：LLM 依据假阴性下结论。应统计超时行数并在结果尾部追加警告。

**7.4 cron 非法 schedule 在运行中的调度器上炸出异常 + 状态不一致** ✅ **已修复 (ca0534a)**
- **修复**:`_validate_schedule` 在 add_job 注册前校验：坏 cron/非整数 interval/interval<=0 → ValueError 无残留。测试 2 个。
- **文件**：`src/microagent/cron/scheduler.py:133`(`add_job`)/`180`(`_schedule_job`)
- **现象**：探针 G-2:`add_job("not-a-cron")` 在调度器已启动时 `CronTrigger.from_crontab`
  抛 ValueError 逃逸给调用方，**且 job 已先写入 self.jobs**（注册了但未调度，状态不一致）;
  G-3:`interval:-5` → APScheduler 接受负 interval(`-1 day, 23:59:55`),`interval:0` 被钳到 1s,
  均无校验；`interval:abc` → int() ValueError 逃逸。

**7.5 同一 session_id 并发 run_turn → 历史交错，resume 出"从未发生的对话"** ✅ **已修复 (9d83601, 方案 A)**
- **修复**:runner 内 `_turn_lock` 串行化 run_turn(wrapper 委托 `_run_turn_inner`);interrupt 无需锁不死锁；跨实例场景文档注明由嵌入方负责。测试 2 个。
- **文件**：`src/microagent/session/runner.py`（无 per-session 并发守卫）
- **现象**：探针 H-1：两个 runner 同 session 并发，双方各自加载空历史 →
  落库顺序 user-A, user-B, assistant-A, assistant-B。每个 turn 的 LLM 上下文都看不到对方，
  但 store 合并了双方 → resume 时模型看到一段它从未参与的组合对话。
- **影响**：嵌入方（gateway 多实例/多 agent 共话）误用时数据完整性受损。
  需要 per-session 锁或文档明确禁止。

### 🔵 可选

**7.6 web_fetch 文档与行为不符（redirect)** ✅ **已修正文档**：docstring 称"每个 redirect 目标都要过同样检查"，
实际 `follow_redirects=False` 后 3xx 响应当普通内容返回（空 body)，并无 redirect 目标检查。
**glob/grep `../` 越界**：探针 F-2/F-3 确认可列出/搜索工作目录外的文件 —— 由 permission 层管控，
属设计行为，仅备注。
