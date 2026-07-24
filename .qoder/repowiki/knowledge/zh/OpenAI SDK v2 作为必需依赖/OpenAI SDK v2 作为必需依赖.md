---
kind: external_dependency
name: OpenAI SDK v2 作为必需依赖
slug: openai-sdk
category: external_dependency
category_hints:
    - vendor_identity
    - sdk_real_api
scope:
    - '**'
---

项目使用官方 openai Python SDK v2（AsyncOpenAI）进行所有 LLM API 调用，包括对非 OpenAI 兼容端点（vLLM、Ollama 等）的调用。SDK 处理 SSE 解析、工具调用增量累积、重试和类型安全。这是必需依赖而非可选扩展，覆盖 base_url 重写以支持任何 OpenAI 兼容端点。