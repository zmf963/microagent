---
kind: external_dependency
name: HTTPX HTTP 客户端
slug: httpx
category: external_dependency
category_hints:
    - client_constraint
scope:
    - '**'
---

HTTPX 用于 web_fetch 工具的 HTTP 请求，具备 SSRF 防护功能。支持同步和异步操作，是 OpenAI SDK 的内部依赖但也被直接用于网络请求。