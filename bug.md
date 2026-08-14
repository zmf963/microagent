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

---

## 八、第六轮疯狂测试（Round 6，探针 I–K）— 2026-08-04

> 方法：探针 I（process 工具）、J（task/subagent）、K（browser 工具）。
> 基线：1061 passed, 11 skipped。

### 🔴 严重

**8.1 `process poll` 对持续输出进程永不返回 — readline 循环无界** ✅ **已修复 (d94520b)**
- **修复**:poll 排空上限 200 行/次 + '(more output pending)' 提示。顺带修复 kill 时 `p.wait()` 对 pipe 大量未读输出挂死（限 5s)。
- **文件**：`src/microagent/tools/builtins/process.py`(poll 分支 `while True` readline 循环）
- **现象**：探针 I-1:`yes spamline` 启动后 poll 挂死 >5s 不返回（每次 0.1s 内必有新行 →
  `while True` 永不 break)，整个工具调用无限期挂起，agent 回合卡死。
- **影响**：任何高频输出进程（yes、tail -f、编译日志）让 poll 变成永久阻塞 → 回合挂死。

**8.2 `process` 输出缓冲无界 — `yes` 数秒即 GB 级内存** ✅ **已修复 (d94520b)**
- **修复**:2000 行环形缓冲 + 单行 2000 字符截断 + dropped 计数，log 披露 `[N earlier line(s) dropped]`。测试 3 个。
- **文件**：`src/microagent/tools/builtins/process.py`(`reg.outputs[sid]` list 无上限）
- **现象**：探针 I-1 中 5s 的 `yes` 输出全部 append 进内存 list（数百万行），进程开始 swap;
  `log` 动作还会把**全部**缓冲塞进 ToolResult。
- **影响**：LLM 启动一个刷屏进程即可 OOM 宿主。需要行数/字节上限 + 截断标记。

**8.3 `browser_navigate` 无 URL 限制 — file:// 任意读本地文件 + 无 SSRF 防护** ✅ **已修复 (506ed09)**
- **修复**:`_check_navigate_url` 启动前校验：http/https scheme 白名单 + 复用 web_fetch `_resolve_and_check` SSRF 封禁表。测试 3 个。
- **文件**：`src/microagent/tools/builtins/browser.py`(browser_navigate)
- **现象**：探针 K-1:`file:///etc/hosts` 导航成功，`browser_snapshot(full=True)` 完整读出
  文件内容（绕过 read_file 的权限层）;K-2:`http://192.168.1.1/` 导航成功，无任何
  SSRF 检查（对比 web_fetch 有完整封禁表）。`javascript:` 被 Playwright 自身拦截（K-3 免修）。
- **影响**：本地文件泄漏 + 内网探测。browser 工具通常需显式启用，但启用后无任何边界。

### 🔵 可选

**8.4 subagent 预算耗尽错误信息重复** ✅ **已修复**(`"budget exhausted: budget exhausted:"`,cosmetic);
**`mcp_connect raw:<command>`** 可执行任意命令 —— 与 bash 工具同级权限，属设计行为，
建议文档注明；**`wait` + 未关闭 stdin 的 `communicate()` 交互**未验证（被 8.1 挂死掩盖，修复后复验）。

---

## 九、第七轮疯狂测试（Round 7，探针 L–N）— 2026-08-04

> 方法：探针 L（lsp 工具，真实 gopls)、M（scrubber 边界）、N(session_search 边界查询）。
> 基线：1068 passed, 11 skipped。

### 🟡 应修复

**9.1 `search_sessions` 空/纯特殊字符查询 → LIKE '%%' 泄漏全部最近消息**
- **文件**：`src/microagent/session/search.py`(search_sessions LIKE fallback)
- **现象**：探针 N:`search_sessions(store, "")` 与 `'"'` 均返回**全部 3 条**消息 ——
  特殊字符剥光后 FTS 查询为 `""` 报语法错 → LIKE fallback 模式为 `'%%'` 匹配所有行。
  与 6.5(recall 空查询）同类。工具层有 `query.strip()` 守卫，但库直调无防护。

**9.2 `lsp symbols` 输出无截断 — 大文件产生巨大 ToolResult**
- **文件**：`src/microagent/tools/builtins/lsp.py`(symbols 分支）
- **现象**：探针 L-1:400 函数 Go 文件 → 10.5KB 未截断输出；5000 符号文件将 >100KB。
  references 有 50 条上限，symbols 没有。

**9.3 `lsp` client 进程死亡后永久缓存 — 不自愈**
- **文件**：`src/microagent/tools/builtins/lsp.py`(`_get_client` / state.clients)
- **现象**：探针 L-2：杀掉 gopls 后，后续所有调用都 `ConnectionResetError('Connection lost')`
  —— 死 client 永远留在 `state.clients[lang]`，无驱逐/重启逻辑，进程级恢复只能重启 agent。

### 🔵 无发现

**scrubber 边界全部正确**（探针 M)：未闭合标签吞后续（文档化设计）、嵌套/分割/大小写/
重复开标签均处理正确。

---

## 九、代码审查修复轮（Round 7, 2026-08-05）— 3 个审查 agent + 手动深读

> 方法：3 个并行审查 agent 分别覆盖核心会话/LLM与技能记忆/工具与基础设施模块，
> 手动深读 + 实测验证交叉确认。基线：1068 passed, 11 skipped。

### 🔴 严重

**9.1 PermissionEngine 是死代码 — 从未被 runner 调用** ✅ **已修复 (7c24b52)**
- **修复**：`SessionRunner.__init__` 接受 `permission_engine` 参数；`_settle` 在 plan-mode
  守卫后、工具执行前调用 `engine.evaluate(call)`；DENY → `ToolResult.denied`。
  `Agent.from_config()` 透传。测试 3 个（DENY/ALLOW/无引擎向后兼容）。
- **文件**：`src/microagent/session/runner.py`、`src/microagent/agent.py`
- **现象**：`PermissionEngine` 只在 `__init__.py` 导出，`runner.py` 零引用。
  LLM 可调 `bash rm *`/`web_fetch`/`execute_code` 零人工确认。
- **影响**：permission bypass — 整个访问控制层无效。

**9.2 output_store 同步写盘阻塞事件循环 + 字符/字节混淆** ✅ **已修复 (dd560f2)**
- **修复**：`process_async()` 用 `asyncio.to_thread` 写盘；阈值检查改用
  `len(content.encode('utf-8'))` 字节数。runner 调用点切换为 async 版本。
- **文件**：`src/microagent/tools/output_store.py`、`src/microagent/session/runner.py`
- **现象**：50KB 工具输出同步写盘阻塞 loop → agent 对 steer/interrupt 无响应；
  多字节 UTF-8 使 50KB 字节阈值实际被 4 倍突破。
- **影响**：LLM 可 DoS agent。

**9.3 memory provider 无 busy_timeout + WAL 顺序错误** ✅ **已修复 (341ddd5)**
- **修复**：`sqlite3.connect(timeout=30)` + WAL 在 schema 前启用。
- **文件**：`src/microagent/memory/provider.py`
- **现象**：并发写 → `database is locked` → `recall()` 静默降级 LIKE → 记忆"丢失"。
- **影响**：多 agent/CLI+agent 并发时记忆系统不可靠。

**9.4 browser 全局 Chromium 进程永不关闭** ✅ **已修复 (29cceac)**
- **修复**：`close_global_browser()` 关闭共享 `_browser` + 停止 `_playwright`；
  `runner.close()` 调用。
- **文件**：`src/microagent/tools/builtins/browser.py`、`src/microagent/session/runner.py`
- **现象**：模块级 `_browser`/`_playwright` 无 close/stop；每次 Agent 创建/销毁泄漏
  headless Chromium 进程。
- **影响**：短生命周期嵌入场景内存泄漏。

### 🟡 应修复

**9.5 FTS5 delete 传 '' 替代实际 content — 索引腐蚀** ✅ **已修复 (341ddd5)**
- **修复**：delete 前 `SELECT content FROM memories WHERE id=?`，传入实际 content。
- **文件**：`src/microagent/memory/provider.py`
- **现象**：外部内容 FTS5 契约要求 delete 带原始文本；`''` 使 delete 为 no-op。

**9.6 子代理 close 父共享 LLM client — ownership 语义不清** ✅ **已文档化 (d6f9028)**
- **修复**：`spawn()` 中显式注释 ownership：`child_runner.close()` 不碰 LLM；
  只有 `forked_llm=True`（spec.model 覆盖）时子代理才关闭自己的 client。
- **文件**：`src/microagent/subagent/manager.py`

### ❌ 剔除的误报（3 项）
- Agent2 #2: ASK 无 callback → fail-open ALLOW — **误报**，返回 ASK 不是 ALLOW。
- Agent2 #5: tool-call deltas 无 finish_reason 丢失 — **误报**，flush 在循环后无条件执行。
- Agent2 #13: `run()` 第二次调用崩溃 — **误报**，实测连续两次 run() 正常。

---

## 十、第八轮疯狂测试（Round 8，3 个并行审查 agent）— 2026-08-09

> 方法：3 个并行只读审查 agent 覆盖核心会话/工具与基础设施/agent-skill-memory-cli 三条线，
> 主会话逐条源码核实后分 4 批修复。基线：1068 passed → 修复后 1097 passed, 1 skipped。
> 注意：Round 7 报告曾将「ASK 无 callback 返回 ASK」判为误报——本轮重新认定为
> fail-open 设计缺陷（runner 只检查 is_deny，ASK 静默放行），见 10.4。

### 🔴 严重

**10.1 L3 压缩 prompt 从未包含 assistant/tool 消息** ✅ **已修复 (ca620ca, 6fbf616)**
- **修复**：`build_compaction_summary_prompt`/`build_incremental_summary_prompt` 注入全对话
  序列化（assistant 含 tool_calls 摘要、tool 结果，单条截 500 字符、总量 30K 从 oldest 截）。
- **文件**：`src/microagent/session/compress.py:372-443`
- **现象**：压缩 LLM 只收到 user 消息枚举（各截 200 字符），第 3/4/7 节（文件/错误/进度）
  必然大量幻觉——压缩金字塔中唯一消耗 API 的层，输出质量从结构上无法保证。

**10.2 CLI `/models refresh` 必然 NameError** ✅ **已修复 (90af17c)**
- **修复**：`surface/cli.py` 顶层 `import asyncio`，删除 main()/_run_streaming 局部 import。
- **现象**：`asyncio` 仅在两个函数局部导入，`_cmd_models` 作用域不可见 → 命令必崩
  （`/models count`/查询正常，测试因此漏网）。

**10.3 MCP 连接失败泄漏子进程 + list_tools 竞态 + 半注册** ✅ **已修复 (f702206)**
- **修复**：`_connected=True` 移到 `list_tools()` 完成后；超时/注册失败先 `disconnect()`；
  `register_tools` 原子化（重名回滚已注册项）；`ToolRegistry.unregister()` 新增。
- **现象**：超时后 `_run_connection` 任务与 npx/uvx 子进程永久泄漏；`_connected` 在
  list_tools 前置位 → 慢 server 注册 0 工具且幂等锁死；重名冲突残留半注册工具表。

### 🟡 应修复

**10.4 PermissionEngine ASK fail-open** ✅ **已修复 (58ce6c5)**
- **修复**：ASK 且无 `ask_callback` → 返回 DENY（reason 注明）。fail-closed。
- **现象**：runner 只检查 `is_deny`，无 callback 时 `rm *`/`mv *`/输出重定向/`task` 等
  ASK 规则被静默放行。

**10.5 attachments 扫描 tool 结果内容 → 提示注入驱动文件外泄** ✅ **已修复 (7842493)**
- **修复**：content 扫描限 user/assistant（tool_calls 参数扫描保留）；读取限 64KB 字节
  再截 3000 字符。
- **现象**：恶意网页/命令输出中写入 `~/.ssh/config` 等路径，L3 压缩后文件被读盘注入
  LLM 上下文；整文件读入内存后才截断，大文件内存尖峰。

**10.6 skill_manage patch/create 无 provenance 校验** ✅ **已修复 (0b8f37a)**
- **修复**：`patch` 加 `_is_agent_created` 检查（与 delete 一致）；`create` 拒绝覆盖
  非 agent 创建的同名 skill。
- **现象**：被注入的 agent 可改写用户手工 SKILL.md（skill 进入 system prompt 链路）→
  持久化 prompt-injection 通道。

**10.7 子 agent close 误杀全局共享 Chromium** ✅ **已修复 (0cdb0a4)**
- **修复**：`close_global_browser()` 从 `runner.close()` 移到 `Agent.close()`。
- **现象**：SubagentManager finally 无条件 `child_runner.close()` → 任何 task 子代理结束
  都关闭进程级共享浏览器，父 agent page 变 "Target closed"，并发会话同死。

**10.8 terminal backend 不处理 CancelledError + Docker 默认必失败** ✅ **已修复 (c3e5b27)**
- **修复**：Local/Docker 加 CancelledError kill+wait+raise；Docker 改 `sh -c`（默认 alpine
  无 bash）；超时/取消后 `docker rm -f` 清残留容器。
- **现象**：中断后子进程孤儿；默认配置 exit 127 必失败；固定 `--name` 撞名后续全挂。

**10.9 LSP 双泄漏：initialize 失败 + 取消后读循环死亡** ✅ **已修复 (2662e92)**
- **修复**：`start()` initialize 失败 terminate 进程+取消任务后 re-raise；`_request`
  取消时 pop `_pending`；`_read_loop` set_exception 前查 `future.done()`。
- **现象**：initialize 失败每次泄漏一个 server 进程且 client 不入缓存（重试再漏）；
  取消后迟到响应对 cancelled future `set_exception` → InvalidStateError → 读循环死亡，
  整个 LSP 会话 30s 超时瘫痪。

**10.10 context_sources/pre_llm_hooks 异常逃逸 run_turn** ✅ **已修复 (17a1864)**
- **修复**：两处均 try/except + log + 跳过（hook 保留上次正常 system prompt），
  对齐 skill_loader 容错模式。
- **现象**：第三方插件异常直接穿透 async generator 崩 turn，连 TurnFailed 都没有。

**10.11 cron `resume:last` 取全局最新 session → 跨 job/用户串扰** ✅ **已修复 (0572e3f)**
- **修复**：每个 job 在 `cron-<name>` 专属 session 下运行（exec lock 串行化、用后恢复），
  resume:last 只加载本 job 历史。
- **现象**：`sessions[0]` 是整个 store 最新 session —— job A 可续上 job B 甚至用户交互
  会话，定时 prompt 注入无关上下文。

**10.12 config.py 非 dict 顶层 YAML 启动崩溃** ✅ **已修复 (1d12d8f)**
- **修复**：`safe_load` 后 `isinstance(data, dict)` 防护，非标量 dict 降级 {} + warning。
- **现象**：合法 YAML 但顶层为标量/列表 → `data.get` AttributeError 启动即崩。

**10.13 resume/失败重试时末尾 user 消息重复写库** ✅ **已修复 (978dc43)**
- **修复**：turn 入口经 `_persist_user_tail` 去重（store 尾部已是该消息则跳过）；
  全部 append 走 `_append()` 维护已知尾部，连续相同用户消息不误伤。
- **现象**：resume 未应答会话或 TurnFailed 后同 list 重试 → store 出现重复 user 消息，
  resume 后模型看到从未发生的对话。

### 📄 文档漂移（本轮同步）
AGENTS.md/README.md/DESIGN.md 数字更新为实测值：runner 977 行、单元测试 1078、
总测试 1098、核心依赖 6、集成测试 10、单测文件 93、核心 ~11,000 LOC。

### 🔵 未处理（下轮候选）
- `_cjk_aware_ratio` 中文匹配限制（4.1 观察项，根治需嵌入检索）；
  流错误重试额外消耗迭代预算（默认 25 次下影响小）；
  question 工具超时后 input 线程抢 stdin（Python 线程根本限制）。

---

## 十二、第十轮修复（Round 10，🔵 候选清单前 5 项）— 2026-08-09

> 基线：1117 passed → 修复后 1127 passed, 1 skipped。

**12.1 memory provider 同步 sqlite3 阻塞事件循环 + rowid 抖动** ✅ **已修复 (b37bf89)**
- **修复**：全部公开 async 方法经 `asyncio.Lock` + `to_thread`（对齐 session/search.py
  纪律），`check_same_thread=False`；`_insert` 弃用 INSERT OR REPLACE——重写时按 delete
  契约清旧 FTS 条目再全新 INSERT；相同 (id, content) 重写为 no-op。
- **现象**：recall 在 turn 中阻塞流式/工具调用；INSERT OR REPLACE 每次重写改 rowid，
  旧 FTS 条目残留（索引膨胀 + rowid 复用后陈旧 token 关联无关记忆）。

**12.5 归一化哈希去重 + 幂等迁移** ✅ **已修复 (b5224f7)**
- **修复**：`_insert` 按 `sha256(strip+空白折叠+小写)[:16]` 去重（不同 id 同内容跳过）；
  标点/语序保留不误杀修订；旧库 `ALTER TABLE` 加列 + 索引 + 存量回填，幂等。
- **现象**：extractor 每轮对重叠窗口产出近似 fact（新 uuid），记忆表无限增长、召回被稀释。

**12.2 snip token 记账低估 → 过度 snip** ✅ **已修复 (9762ca4)**
- **修复**：提取 `_message_tokens()` 共享 helper（content + tool_calls 序列化 + 4），
  count_tokens 与 snip 共用。
- **现象**：snip 按 content-only 递减 total_tokens（实际含 tool_calls+开销）→ 每次删除
  低估释放量 → 删掉比需要更多的 tool 结果。

**12.3 MCP 超时路径异常遮蔽** ✅ **已修复 (9a0ac70)**
- **修复**：轮询循环检查 `self._task.done()`，立即重抛任务真实异常。
- **现象**：server 启动即失败（npx 不存在等）仍白等 5s，真实错误被 "timed out" 遮蔽。

**12.4 lsp symbols 输出无截断** ✅ **已修复 (0d80110)**
- **修复**：上限 200 条 + 总数 + 截断提示（对齐 references 模式）。
- **现象**：5000 符号文件产生 >100KB 无界 ToolResult。

---

## 十一、第九轮疯狂测试（Round 9，3 个并行审查 agent + 复审本轮修复）— 2026-08-09

> 方法：(a) 复审 Round 8 的 15 个 commit 是否引入新问题；(b) 深挖此前未覆盖区域
> （budget/compress L1L2/security/memory/pool/output_store）；(c) 核实 🔵 积压项。
> 基线：1097 passed → 修复后 1117 passed, 1 skipped（一次偶发 flake，复查三轮全绿）。

### 🔴 严重

**11.1 子 budget 自身耗尽误杀整个 budget 树** ✅ **已修复 (1469bb7)**
- **修复**：只有 root 耗尽或 tree 耗尽才 set 共享 cancel_event；子节点自身耗尽只本地 raise。
- **文件**：`src/microagent/session/budget.py:151-160`
- **现象**：explore 子代理跑满自己的 10 次迭代上限 → set 共享 root cancel_event →
  父 runner 下一次 consume() 抛 "budget cancelled by root"，后续子代理全部拒绝启动。
  文件头契约明确 "shared cancel_event for root exhaustion"。

**11.2 micro_compact 无尾部保护 → LLM 永远读不到文件内容的死循环** ✅ **已修复 (863355f)**
- **修复**：最近 4 条 tool 结果不截断（对齐 snip keep_recent 契约）。
- **文件**：`src/microagent/session/compress.py:187-238`
- **现象**：压缩检查在每轮迭代开头执行；60-80% 上下文区间的长会话中，刚产生的
  工具结果被换成占位符，LLM 按占位符提示重读文件，新结果又被截断 —— 不可打破的
  重读循环烧光预算。

### 🟡 应修复

**11.3 `_cached_tools` 不随 registry 变化刷新** ✅ **已修复 (2584538)**
- **修复**：`ToolRegistry` 加单调 version 计数器，runner 版本不一致即重建快照。
- **现象**：mcp_connect 会话中注册的工具永远不进 LLM tools 列表（MCP 核心场景失效）。

**11.4 cron 换 session_id 与进行中交互 turn 竞争 + 去重状态跨 session 污染** ✅ **已修复 (6663287)**
- **修复**：run_turn 在 turn 锁内一次性捕获 sid 并贯穿整个 turn（store append/
  output_store 路径/turn_complete 事件）；`_store_tail`/`_tail_checked` 按 sid 键控。
- **现象**：cron tick 与交互 turn 重叠时，交互 turn 后续消息全部写入 cron 会话；
  去重状态被 cron 会话尾部污染导致 10.13 场景复活。

**11.5 CLI 从未接 PermissionEngine；task 工具 fail-closed 后无法批准** ✅ **已修复 (21651fa)**
- **修复**：`_make_agent` 挂 DEFAULT_RULES + rich Prompt 交互 ask_callback（y→ALLOW）。
- **现象**：permission.py 注释声称 "CLI/Web injects one" 但 CLI 没接；嵌入方传裸
  engine 则 task/rm/mv 永远 DENY 无恢复路径。

**11.6 FTS5 CJK 查询静默空结果（实测复现）** ✅ **已修复 (40a1c34, 08fb0fd)**
- **修复**：CJK 查询整体走 LIKE 子串路径（search_sessions 按词 AND；recall 走现有
  LIKE fallback）。首轮 bigram* 前缀方案实测只命中 run 首 bigram，二次修复改 LIKE。
- **现象**：unicode61 把 CJK 整段索引为单 token，"代码" 永配不上 "用户的代码审查…"，
  无报错 → LIKE fallback 不触发 → 错误空结果。

**11.7 凭证轮换泄漏 httpx client + 轮换后重试无 backoff 只试一次** ✅ **已修复 (2fdfc13)**
- **修复**：`_on_auth_error` async 化并 close 旧 client；轮换重试走
  `_create_with_backoff`，最多 3 次轮换。

**11.8 exit 工具 `[SESSION_EXIT]` 标记无人消费** ✅ **已修复 (7f0c52f)**
- **修复**：runner 在工具结果落库后检测标记 → TurnComplete 结束 turn。
- **现象**：exit 工具契约完全未实现，调用成 no-op。

**11.9 注入扫描未闭合标签绕过** ✅ **已修复 (c4a4c88)**
- **修复**：三种标签族补开/闭单标签 pattern。
- **现象**：未闭合 `<context>` 穿过扫描，而 runner 自己用 `<context>` 包裹注入
  context —— 走私标签之后的内容全被重标记为可信 runner 上下文。

**11.10 小修批（10 项）** ✅ **已修复 (7c9ad05)**
EventBus.emit 改 gather 并发；溢出恢复压缩用 auxiliary_model；Budget.reset 清
cancel_event；lsp 死 client 驱逐重建；CredentialPool.mark_ok 成功重置失败计数；
steer callback 不再自抛 CancelledError；Curator.run_once 容忍缺失 skills_dir；
CLI session_id 加 uuid 后缀；context7 截断响应返回原文前缀不再必失败；
Agent.close 接线 cleanup_expired；删 runner 同步死代码 `_process_tool_output`。

**11.11 memory extractor 用主模型 + 输入无界** ✅ **已修复 (c54d56c)**
- **修复**：auxiliary_model 优先；prompt 单条 2K/总量 20K 截断。
- **遗留**：重叠窗口近似重复记忆增长（需 provider 内容哈希去重）记入下轮候选。

### 复审结论（Round 8 commit 复查）
- 978dc43/0572e3f 的交互缺陷（11.4）已修；7842493 UTF-8 截断无乱码注入
  （errors="replace"）；其余 12 个 commit 未发现引入性 bug。
- mcp 超时路径异常遮蔽（f702206 残留 🔵）记入候选清单。

---

## 十三、第十一轮修复（Round 11，剩余 🔵 清单）— 2026-08-11

> 基线：1127 passed → 修复后 1128 passed, 11 skipped。

**13.1 pricing 裸 dated id 前缀匹配失效** ✅ **已修复 (7d094e2)**
- **修复**：`_lookup()` 第三步增加 bare-tail-prefix 条件——查询无 `/` 时，
  用 cache key 的 tail（`/` 后部分）做前缀匹配。
- **现象**：`gpt-4o-2024-08-06`（无 `openai/` 前缀）穿透到 fallback 价格。

**13.2 SSH AutoAddPolicy 无主机密钥校验** ✅ **已修复 (edf3d2d)**
- **修复**：新增 `known_hosts` 参数——`None`/`True`（默认）加载 `~/.ssh/known_hosts`
  + RejectPolicy；`False` 跳过验证（旧行为）；`str` 指定文件路径。
- **现象**：始终 AutoAddPolicy，MITM 风险。

**13.3 LocalTerminal env 整体替换** ✅ **已修复 (e474271)**
- **修复**：`env` 参数合并到 `os.environ.copy()` 之上，不再整体替换。
- **现象**：传 `env={"FOO": "bar"}` 导致 PATH 丢失，子进程找不到基本命令。


---

## 十四、第十二轮疯狂测试（Round 12，3 并行审查 agent + 探针验证）— 2026-08-14

> 方法：3 个 explore agent 分模块组深读（core/session、tools/terminal/mcp/cron、memory/skill/llm/cli），
> **每项发现都用探针/读源码逐条验证**（剔除误报 ~9 项）。基线 1128 passed → 修复后 **1134 passed, 1 skipped**。

### 🔴 严重（全部已修复）

**14.1 FTS5 session 搜索从未真正工作** ✅ **已修复 (c8cda65)**
- 双重根因：(a) `messages_fts` 用 `content=messages` external-content 模式，但 messages 表无
  `role/content/session_id` 列（在 JSON `data` 里）→ 任何 MATCH 查询抛 `no such column: T.role`；
  (b) 查询 `SELECT data FROM messages_fts` 引用了 FTS 表不存在的列。两层 `except` 静默吞掉，
  永远退回 LIKE。两次 /tmp 探针实测复现。修复：自包含表 + `rowid→messages.id` JOIN +
  `ensure_fts5` 自愈迁移（检测旧 schema 并 drop+rebuild+backfill）。

**14.2 `browser_get_images` JS 死工具** ✅ **已修复 (7e05fa4)**
- `browser.py:422` `results.push({{` 三引号非 f-string，字面 `{{` 传给 JS → node 实测
  `Unexpected token '{'`，每次调用必失败。改回单大括号。

**14.3 vision 结果被 output_store 静默截断** ✅ **已修复 (af24aa6)**
- `runner.py` 对所有工具结果无条件走 `_process_tool_output_async`；>50KB 的 base64 截图被
  head/tail 500 字符预览替换，LLM 收到坏图片无报错。新增 `_OUTPUT_STORE_EXEMPT`
  （browser_vision/vision_analyze）豁免。

**14.4 CLI `/new` `/resume` `/compact` 复用已关闭的 store** ✅ **已修复 (453b909)**
- `Agent.close()` 关 store（library 不泄漏连接），但 CLI 三条命令复用同一 store 实例 →
  下次写库 `sqlite3.ProgrammingError`。修复：ReplState 记 db_path，`agent.close()` 后
  `_reopen_store()` 在同路径重开（WAL 保证新连接可见历史）。

### 🟡 应修复（全部已修复）

**14.5 execute_code 内存无界 + 孙进程孤儿** ✅ **已修复 (433dc5c)**
- `communicate()` 全量缓冲后再截断 → `while True: print('x'*10**6)` 在 timeout 前 OOM；
  且无 `start_new_session`，超时 kill 只杀 python 父进程。改为 bash.py 同款流式读取 +
  killpg。

**14.6 process wait 动作 `communicate()` 无超时** ✅ **已修复 (05f3057)**
- `p.wait()` 后 `p.communicate()` 无超时，孙进程占管道时永久挂起（kill 动作已有 5s bound，
  wait 漏了）。加 5s bound。

**14.7 mcp_connect 幂等竞态 + 死连接不重连** ✅ **已修复 (5689685)**
- 检查非原子：并发 TaskGroup 两次 `mcp_connect("git")` 都过检查 → 泄漏 npx 进程；
  且 manager 一旦入 dict 永不清理，server 崩溃后永远 "already connected"。加 per-session
  Lock 串行化 check+connect；`_task.done()` 时清理重连。

**14.8 skill loader 每 turn 全量重扫 + 阻塞事件循环** ✅ **已修复 (4cf861b)**
- `load()` 是 async 但同步 rglob+read_text 跑在事件循环，且 runner 每 turn 调用最多 3 次
  （catalog + match + bodies）。加 mtime 指纹缓存 + `run_in_executor` 卸载到线程。

**14.9 skill loader YAML frontmatter 非 dict 中断 load** ✅ **已修复 (466d9a6)**
- `safe_load` 返回 list/scalar 时 `front.get` 抛 AttributeError（非 YAMLError 未捕获），
  一个坏 SKILL.md 拖垮全部技能加载。加 isinstance 守卫（对齐 config.py）。

**14.10 cron scheduler 顶层 `import fcntl` 无守卫** ✅ **已修复 (3f2ac9a)**
- `__init__.py` 无条件导入 cron.scheduler → Windows 上 `import microagent` 直接 ImportError
  （readline/termios/tty 已有守卫，fcntl 漏了）。改 try/except，无 fcntl 时降级为 no-op 锁。

### 剔除的误报（已逐项验证，不报告）

| 原 claim | 验证结果 |
|----------|---------|
| budget 耗尽 TurnFailed 被丢弃 | ❌ `arun` 的 async for 正常消费 |
| `_persist_user_tail` store=None 崩溃 | ❌ runner.py:431 有 `if self.store is not None` 守卫 |
| budget 子节点能 set 共享 cancel_event | ❌ 即 11.1 修复后的 `_tree_exhausted()` 设计契约 |
| 流重试 `continue` 无限/泄漏 | ❌ `_stream_retried` 一次性保护，逻辑正确 |
| `_process_tool_output` 丢 denied metadata | ❌ 347 行 `metadata=result.metadata` 保留（vision 截断是独立项 14.3） |
| `_watch_esc` 线程不安全 | ❌ 全在事件循环线程，无跨线程写 |
| tx-d4p 模板"与注释不符" | ❌ skill 已验证 `'Flash' not in template('tx-d4p')` 是预期 |
| 凭证轮换 service_tier/reasoning_effort 丢失 | ⚠️ 真实但极边缘（pool 通常同模型族），降级未修 |
| memory sync_turn mem_id 时间戳碰撞 | ⚠️ 真实但内容哈希去重已缓解，降级未修 |

### 遗留未修（🔵，非本轮目标）
- CJK 技能匹配仍字面匹配（需 embedding 语义检索）
- 流错误重试消耗迭代（默认 25 次下影响极小）
- question 超时后 input 线程无法 kill（Python 限制）

---

## 十五、第十二轮遗留项处理（Round 12b，2026-08-14）

> 处理 bug.md 第十四轮末尾记录的 3 个 🔵 遗留项。基线 1134 passed → **1142 passed, 1 skipped**。

### 15.1 流错误重试消耗迭代 ✅ **已修复 (a83ef48)**
- **现象**：重试 pass 重新进入外层循环，命中循环顶部的无条件 `budget.consume(iterations=1)`——
  一次一次性重试为**同一次逻辑 LLM 调用**消耗 2 个迭代。网关抖动时默认 25 次预算实际
  只剩 ~12 次真实调用。
- **修复**：`_stream_retry_free` 标志在重试分支置位，下一次循环 pass 跳过 consume 恰好一次
  （turn 开始时与 `_stream_retried` 一起重置）。重试不产生新输出，免费才是正确记账。

### 15.2 question 工具 input 线程与 ESC watcher 冲突 ✅ **已修复 (c3017bd)**
- **现象（比"线程无法 kill"更严重的真实 bug）**：watcher 整轮处于 cbreak 模式——
  (a) input() 的 ICANON 被关，readline 敲第一个键就返回，答案被截断成单字符；
  (b) watcher 的读线程和 input() 竞争同一 stdin fd，持续偷走用户按键。
- **修复**：
  - question 工具置位线程安全 `_QUESTION_ACTIVE` 标志；watcher 改为 0.2s 有界读轮询该
    标志，提问期间暂停读键。
  - watcher 启动时把原始 cooked termios 设置发布给 question 工具；question 在 input()
    前**自己恢复 cooked 模式**（不等 watcher 轮询到，消除答案开头被截断的窗口）。
  - 超时提示用户按 Enter 清理滞留线程（Python 无法 kill 线程——这是语言硬限制，
    现已无害且可恢复）。

### 15.3 CJK 技能匹配语序不敏感 ✅ **已改进 (bea0e37)**
- **现象**：bigram **集合**覆盖无法区分"测试驱动"与"驱动测试"——同样 bigram 倒序得分相同，
  语序在中文里改变语义。
- **改进**：新增有序 bigram **子序列**覆盖（LCS over bigram sequences，允许间隙），
  混合进 `max(coverage, lcs_ratio, subseq_ratio)`。不相关查询仍 0.0 零误报；
  同序匹配高于倒序；容忍自然语言查询中的同义插入。
- **仍遗留**：完整语义检索需要 embedding 模型——超出零依赖范围，文档已注明。

---

## 十六、第十三轮疯狂测试（Round 13，3 并行审查 agent + 全部探针验证）— 2026-08-14

> 方法：3 个并行审查 agent 分模块组深读（runner/agent/core、tools/terminal/mcp、
> llm/memory/skill/cli/cron），每项发现逐条探针实测后再修。
> 基线 1142 passed → 修复后 **1155 passed, 1 skipped**。
> 8 个 fix commit：`a42cbd0`/`e201903`/`1f1ba50`/`a12620f`/`64e30ce`/`99e2f4e`/`18e3920`/`6a3e39c`。

### 🔴 严重（全部已修复 + 探针验证）

**16.1 mcp_connect 幂等锁失效 — 并发调用全部 spawn 子进程** ✅ **已修复 (a42cbd0)**
- **现象**：Round 14.7 的"原子幂等检查"修复实际无效——`session_state` 惰性创建的
  `asyncio.Lock` 是 **per-task** 的：anyio `start_soon` 给每个 `_settle` 任务新 context，
  每个任务惰性创建自己的锁。探针：一个 turn 内 3 个并发 `mcp_connect` 调用 → 3 次
  `connect_mcp_stdio`（应为 2）→ 多 spawn 的 npx/uvx 子进程被覆盖为孤儿。
- **修复**：runner 持有单一 `_mcp_connect_lock`，`_settle` 内 per-task 绑定
  （与 `_current_managers` 同模式）。探针复验：3 并发 → 2 spawn ✅。

**16.2 budget 在 consume_usage 耗尽 → store 留孤儿 tool_calls** ✅ **已修复 (a42cbd0)**
- **现象**：assistant(tool_calls) 在 `consume_usage` 之前已持久化；BudgetExceeded
  直接 return → store 状态 `assistant(tool_calls=[c1])` 无对应 tool 结果 → OpenAI
  resume 时拒绝（"messages must contain tool results for all tool calls"）。
  探针复现：`ORPHANED: ['c1']`。interrupt 路径早有此保护（persist error results），
  budget 路径漏了。
- **修复**：为每个 tool_call 持久化 error 结果 + 内存中 assistant 消息剥除 tool_calls。
  探针复验：`ORPHANED: []` ✅。

**16.3 `process write` 无界阻塞 — 非读 stdin 进程挂死整个回合** ✅ **已修复 (e201903)**
- **现象**：`sleep 100` 等不读 stdin 的进程 + 10MB payload（data 字段无大小上限，
  LLM 回显大文件即可触发）→ 管道填满后 `p.stdin.drain()` 永久阻塞。
  探针：`WRITE HUNG CONFIRMED`。
- **修复**：`wait_for(drain, 5s)`，超时返回"process is not reading stdin"错误。
  探针复验：5.0s 返回错误 ✅。

**16.4 `runner.close()` 挂死 — killpg 后 `await proc.wait()` 永不返回** ✅ **已修复 (a42cbd0)**
- **现象**：asyncio 的 `proc.wait()` 要等管道 transport 排空才 resolve；`yes`/`tail -f`
  刷满管道后 kill，无 reader → `Agent.close()`/CLI 退出永久挂起。探针：`CLOSE HUNG CONFIRMED`。
- **修复**：`wait_for(proc.wait(), 5s)` 有界等待（进程已死，等待只是管道排空）。
  探针复验：close() 正常完成 ✅。

### 🟡 应修复（全部已修复）

**16.5 mcp 死 server 重连必失败（duplicate tool）** ✅ **已修复 (a42cbd0)**
- 死 manager 的 adapter 永远留在 registry；新连接 `register_tools` 首个同名工具即
  `ValueError("duplicate tool")`。`disconnect()` 现可注销自己注册的工具
  （`_registered_tool_names` 记录），mcp_connect 重连路径传 registry。

**16.6 LocalTerminal/DockerTerminal 管道满 kill 挂死** ✅ **已修复 (e201903)**
- 探针：`sh -c 'sleep 300 & sleep 30'`（孙进程持管道写端）超时后 `proc.wait()` 永不返回。
  新增 `_wait_killed()`：排空缓冲 + 有界等待。探针复验：timeout 路径正常返回 ✅。

**16.7 browser redirect SSRF** ✅ **已修复 (1f1ba50)**
- `page.goto()` 跟随 redirect，但预检只看初始 URL——公网 URL 302 到
  `169.254.169.254`/RFC1918 即加载内网内容。goto 后复检最终 URL，落在封禁段
  即关闭页面并拒绝。

**16.8 plan 模式放行 git 工具 + mcp_connect raw** ✅ **已修复 (a12620f)**
- git 工具白名单含 commit/add（改写仓库）；`mcp_connect raw:<command>` 执行任意命令。
  均加入 `_PLAN_BLOCKED_TOOLS`（执行层硬拦截 + LLM 工具清单过滤）。

**16.9 `/skill unload` 是死命令** ✅ **已修复 (a12620f)**
- 名字只进了 `ReplState.disabled_skills`，无人消费——被卸载的 skill 每轮照常注入
  system context。runner 现持有 `disabled_skills`，匹配与注入双重过滤；CLI 推送并
  清除已加载条目。

**16.10 write_file 备份读无上限 + 同步 IO 阻塞事件循环** ✅ **已修复 (64e30ce)**
- 备份路径 `read_bytes()` 整读已有文件（新内容 10MB 封顶但旧文件没封）→ 多 GB 文件
  OOM。加 10MB 拒绝；整块 IO 移入 `asyncio.to_thread`。

**16.11 attachments 从可注入文本读取系统文件** ✅ **已修复 (99e2f4e)**
- 用户/助手消息文本可被 prompt-injected；命名 `/etc/hosts` 等系统路径即被读盘送
  LLM API（tool 结果已排除，但 user/assistant 内容扫描是漏洞）。文本扫描路径新增
  `_is_system_path` 拒绝（/etc、/var/log、/root 等）；工具调用参数保留完整路径空间
  （已过权限层）。

**16.12 web_fetch DNS rebinding TOCTOU** ✅ **已文档化 (18e3920)**
- resolve-then-connect 存在窗口：httpx 连接时二次解析，split-horizon DNS 可绕过。
  彻底修复需 SNI/Host 钉住的自定义 transport——超范围，docstring 如实注明。

### 剔除的误报（已逐项探针验证，不报告）

| 原 claim | 验证结果 |
|----------|---------|
| interrupt 丢已完成工具结果 + 孤儿 tool_calls | ❌ 探针：`_settle` 的 finally 持久化 error 占位，store 无孤儿（`not executed (cancelled)`） |
| user steer 泄漏进 cron 会话 | ❌ 探针：steer 后 cron session 历史无泄漏文本（steer 仅注入内存中的 tool 消息） |
| memory `_insert` 改内容重写抛 IntegrityError | ❌ 探针：external-content FTS5 值插入完全合法，现有测试 `test_rewrite_same_id_clears_stale_fts` 通过 |
| Esc×2 计数永远到不了 2 | ❌ 步进模拟：两次 Esc 间隔 <0.5s 时计数正确到达 2（重置只发生在 sleep 之后） |
| `_persist_user_tail` store=None 崩溃 | ❌ 有 `if self.store is not None` 守卫 |
| search.py 孤儿触发器致 append 必败 | ❌ `CREATE TRIGGER IF NOT EXISTS` 覆盖重建，`_has_broken_fts_schema` 的 drop 路径同时删触发器 |

### 遗留未修（🔵，非本轮目标）
- CLI `_watch_esc` 每次 poll 泄漏一个阻塞的 to_thread 读线程（stdin 读不可 kill；
  长流期间线程堆积，后续可用专用常驻 reader 线程修复）
- `LocalTerminal._wait_killed` 有界等待仍不读尽管道（macOS 上已验证无影响）
- mcp_connect `_task=None` 的 manager 被视为 live——`_task` 未初始化即幂等跳过
  （正常路径无此形态，防御性处理留待下轮）

---

## 十七、第十四轮：memory 默认开启 + /learn 技能沉淀（Hermes 对齐）— 2026-08-14

> 方法：参考 Hermes Agent（实测本机 `~/.hermes` 安装 + 官方 skill 文档）后对齐设计。
> Hermes 哲学：**技能沉淀是刻意行为（/learn），不是自动后台循环**——例行维护
> 零 token；memory 默认开启、每轮注入、cron 隔离。
> 基线 1155 passed → 修复后 **1175 passed, 1 skipped**。
> 6 个 feat commit：`46b6a9f`/`512f439`/`c7e76c0`/`d7697b8`/`c88e301`/`b8c0d35`。

### 16.1 memory 默认开启 + 每轮注入 ✅ (512f439)
- `Agent.from_config(memory=True)`（默认）构造 `SQLiteMemoryProvider(~/.microagent/memory.db)`
  + LLM extractor（auxiliary model 优先）；`memory=False` 关闭 / 实例直用。
- runner 每轮 `recall(last_user, k=5)` 注入 context block（与 skill 同通道，
  过注入扫描）；recall 失败静默降级。
- `Agent.close()` 关闭 provider 连接（不再泄漏 SQLite 连接）。

### 16.2 write_approval 闸门 ✅ (46b6a9f)
- 对齐 Hermes `write_approval`（默认 false = 直写）：True 时 batch_write 进
  `pending_memories` 表，`approve_memory`/`reject_memory` 驱动闸门。
- CLI `/memory [pending|approve <id>|reject <id>]`（Hermes /memory parity）。

### 16.3 记忆滚动上限 ✅ (46b6a9f)
- `MAX_MEMORIES = 500`：insert 超限驱逐最旧（category='context' 优先——原始
  会话回响最不耐久）。Hermes 用 char_limit + LLM 压缩，SQLite 形态用行数语义。

### 16.4 cron skip_memory 隔离 ✅ (512f439)
- runner 新增 `skip_memory`；cron `_execute_job` 置位/恢复——后台 tick 不注入
  记忆上下文、不写记忆（Hermes 不变式："cron sessions pass skip_memory=True"）。
- 子代理不继承 parent memory（SubagentManager 构造未传）——上下文防火墙语义保持。

### 16.5 /learn 技能沉淀（chat/dir/url）✅ (c7e76c0, c88e301)
- 新模块 `skill/learner.py`：一次性蒸馏——auxiliary model 优先生成 SKILL.md，
  写入 `~/.microagent/skills` + `.provenance.json(agent)` + curator usage 条目
  （与 skill_manage 同磁盘形态，Curator 统一管理生命周期）。
- `Agent.learn(source, kind=)` 库级 API；CLI `/learn chat .` 从**当前会话**学习。
- `ClaudeSkillLoader.invalidate_all()`：learn/create/delete 后立即可匹配。
- **agent-created skills 目录（~/.microagent/skills）现在始终在默认搜索路径**——
  之前 skill_manage 写进去的 skill 从未被加载（需手动配 skills_path）。
- 安全：name 过 `is_safe_name`、URL 走 SSRF 检查、同名拒绝覆盖。

### 16.6 curator 归档备份 + pin ✅ (d7697b8)
- 归档前 tar.gz 备份到 `.archive/`（Hermes parity：永不丢失，rename 非 delete）。
- `.usage.json` 支持 `pinned`；`Curator.set_pinned()`；skill_manage 的 usage
  touch 保留 pinned。

### 与 Hermes 的差异（有意为之）
| 维度 | Hermes | MicroAgent（本轮） |
|------|--------|-------------------|
| 记忆存储 | MEMORY.md/USER.md 文件 | SQLite+FTS5（保留,库场景多进程安全） |
| 记忆抽取 | 工具驱动 | 每轮自动 LLM 抽取（保留,更先进） |
| 上限 | char_limit + LLM 压缩 | 500 行驱逐 |
| 沉淀触发 | /learn（人工） | /learn（人工,对齐） |
| 例行维护 | 零 token | 零 token（对齐） |
| LLM 密集维护 | consolidate opt-in | 未实现（候选:重叠技能合并） |
