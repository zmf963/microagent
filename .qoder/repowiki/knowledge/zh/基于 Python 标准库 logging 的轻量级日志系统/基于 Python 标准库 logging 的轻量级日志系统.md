---
kind: logging_system
name: 基于 Python 标准库 logging 的轻量级日志系统
category: logging_system
scope:
    - '**'
source_files:
    - src/microagent/cron/scheduler.py
    - src/microagent/mcp/client.py
    - src/microagent/memory/extractor.py
---

MicroAgent 项目使用 Python 标准库 `logging` 模块作为唯一的日志系统，未引入第三方日志框架（如 loguru、structlog）。各模块通过 `logging.getLogger(__name__)` 获取模块级 logger 实例，采用最简化的配置方式——未在代码中调用 `logging.basicConfig` 或任何 Handler/Formatter 配置，因此日志输出完全依赖运行时的默认配置。

日志级别使用模式：
- `logger.info()`：用于关键业务流程事件，如 cron 任务完成
- `logger.error()`：用于异常和失败场景，如 cron 任务执行失败
- `logger.debug()`：用于调试信息，如内存提取失败

当前使用情况：仅 3 个文件使用了 logging：
- `src/microagent/cron/scheduler.py`：记录 cron 任务的执行结果和错误
- `src/microagent/mcp/client.py`：定义了 logger 但未实际使用
- `src/microagent/memory/extractor.py`：记录内存提取失败的调试信息

架构特点：无集中式日志配置入口，每个模块独立管理自己的 logger；无结构化日志字段，日志消息以字符串格式化为主；无日志级别控制机制，无法通过配置文件动态调整；CLI 入口也未进行任何日志初始化。这种设计使得日志系统处于可用但简陋的状态，适合小型工具类应用，但在生产环境中缺乏必要的可观测性能力。