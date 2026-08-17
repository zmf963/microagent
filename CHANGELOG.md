# Changelog

## 1.1.2 (2026-08-16)

第二十一轮审查修复批——v1.1.x 新代码 + 交叉组合 + CLI/配置/打包面。

### Fixed
- **body_invoked 分类损坏**:工具主体抛异常时标志残留 True,中断窗口内
  错误结果被误标 `ABORTED`(重放会拒绝重跑从未派发的工具)。错误路径复位。
- **子代理沙箱/权限逃逸**:子代理未继承 `terminal_backend`/`permission_engine`,
  且每轮重绑 backend=None——Docker/SSH 隔离的父代理,子代理 bash 在**主机**
  执行;权限引擎同样被丢弃。两者现在继承。
- **/model 提取器凭据陈旧**:切换模型后 MemoryExtractor 仍调用旧端点/
  旧 key/旧 model。重建提取器;`retry_policy` 保留。
- **REPL 无异常守卫**:未来版本会话行(/resume 触发
  `UnsupportedSessionError`)杀死整个 REPL。分发处守卫 → 错误面板。
- **exit-tool 路径跳过 flush 屏障**;`always:N` 重试被一次性重试门
  静默降级为 1 次(计数替代布尔)。
- `RetryPolicy.from_str` 非法规格静默转 normal → 现 raise;
  `llm_retry` 账本无界 → 每会话修剪 100 行。
- bash 后端 stderr `endswith` 误去重(丢弃独立 stderr)→ 标记段合并。
- CompositeSkillLoader first-wins 遮蔽高分匹配 → 保留最高分。
- `pricing.refresh` 丢弃免费模型(pricing:null)→ 保留 (0.0, 0.0),
  不再翻成 $0.50 fallback 误触 BudgetExceeded。
- config 平铺布局静默忽略 → 响亮告警;`auxiliary_model`/
  `reasoning_effort`/`service_tier`/`retry_policy` 支持 env/文件。
- 打包元数据:过期描述、PyPI 非法 `.local` URLs、硬编码 v1.0.0 横幅。

## 1.1.1 (2026-08-16)

absorb-later 低风险三项落地:flush 屏障、每-provider 重试策略、
bash TerminalBackend 接缝。

### Added
- **flush 屏障**(dsh `session/flush` parity):`Store.flush(session_id)`
  (SQLite: WAL PASSIVE checkpoint;InMemory: no-op);runner 在
  TurnComplete 前 flush——完成后立即崩溃也不丢最后一轮。自定义
  store 无 flush() 时跳过,flush 失败静默降级。
- **RetryPolicy**(dsh retry-policy parity):`RetryPolicy(mode=normal|
  always|never, max_retries)` 随 LLMConfig 路由携带——全局可重试词表
  无法表达"此网关 500 恢复快,激进重试"vs"此 provider 的 500 是 bug,
  不重试"。`LLMConfig.retry_policy` 接受字符串规格或对象。顶层导出
  (68 符号)。
- **bash TerminalBackend 接缝**:`bash_current_backend` ContextVar;绑定
  后 bash 经 TerminalBackend 执行(docker 隔离/SSH 远程),TerminalResult
  翻译为 bash 契约(exit-code 后缀、超时部分输出、后端异常→工具错误)。
  `SessionRunner(terminal_backend=)` / `Agent.from_config(terminal_backend=)`
  per-task 绑定——换后端即迁移整个能力族,不改工具本身。

### Changed
- runner 流重试改由 `resolved_retry_policy()` 决策(替代裸
  `is_retryable`);'never' 的 provider 不再烧一次性重试。

## 1.1.0 (2026-08-16)

deepseek-harness 吸收批次 — absorb-now 四项 + 发布整理。

### Added
- **取消双码分类**(dsh `bodyInvoked` parity):interrupt 的工具结果带
  `metadata.code` — `ABORTED_BEFORE_DISPATCH`(主体未运行,可安全重跑)
  vs `ABORTED`(主体已运行后取消,重跑可能重复副作用)。重放/重试逻辑
  从此可程序化区分。`ToolResult.error` 支持 `metadata=` 参数。
- **未知会话事件拒绝**(dsh `ignorable`-defaulted-required parity):
  序列化行新增 `kind` 字段;`UnsupportedSessionError` 在遇到未知且非
  ignorable 的 kind 时让 `load_history` 响亮失败,未来版本会话不再被
  静默误读。旧库行(无 kind)保持可读。顶层导出(67 公共符号)。
- **LLM 重试账本**(dsh retry-history-from-log parity):`llm_retry` 表 +
  `record_llm_retry`/`last_llm_retry`(SQLite 与 InMemory 双实现);
  runner 每次一次性流重试落账(provider Retry-After 优先,否则 1s)——
  退避续接与重试审计跨进程重启存活。
- **LLMFailure provider 提示**:`retry_after_ms` + `request_id` 从异常
  headers 提取(openai `.headers` / httpx `.response.headers`),贯穿
  全部分类分支——完整 dsh LlmFailure 对齐。

### Changed
- Store Protocol 扩展两个 retry 方法(自定义 store 需补齐实现)。

## 1.0.0 (2026-07-31)

初始版本 — 18 轮审查迭代后的基线。可嵌入 AI agent 核心库:
34 内置工具、4 层压缩金字塔、FTS5 记忆、技能加载与 /learn 沉淀、
curator 生命周期、cron 调度、MCP 客户端、browser/lsp 工具族。
