---
kind: external_dependency
name: APScheduler 定时任务调度器
slug: apscheduler
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

APScheduler 用于实现 Cron 定时任务功能，支持 cron 表达式和间隔调度。通过 CronScheduler 类管理任务的添加、启动和停止。可选依赖 microagent[cron]。