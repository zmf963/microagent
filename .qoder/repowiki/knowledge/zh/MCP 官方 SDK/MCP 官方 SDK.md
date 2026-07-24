---
kind: external_dependency
name: MCP 官方 SDK
slug: mcp-sdk
category: external_dependency
category_hints:
    - vendor_identity
    - framework_behavior
scope:
    - '**'
---

使用官方 mcp Python SDK 实现 MCP 客户端功能，而非手写 JSON-RPC + 传输实现。将 MCP 客户端代码从 ~400 LOC 减少到 ~50 LOC，委托协议复杂性（JSON-RPC 生命周期、传输协商、工具列表变更通知、OAuth）给维护良好的 SDK。可选依赖 microagent[mcp]。