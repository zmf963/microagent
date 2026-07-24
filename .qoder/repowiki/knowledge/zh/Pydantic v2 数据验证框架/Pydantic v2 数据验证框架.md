---
kind: external_dependency
name: Pydantic v2 数据验证框架
slug: pydantic
category: external_dependency
category_hints:
    - framework_behavior
scope:
    - '**'
---

Pydantic v2 作为必需依赖用于数据验证和配置管理。Config 类使用 dataclasses 和 Pydantic 进行多源配置解析（CLI > 环境变量 > 配置文件 > 默认值），确保类型安全和配置完整性。