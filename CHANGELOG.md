# Changelog

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
