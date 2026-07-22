# Use official mcp SDK instead of hand-rolling MCP client

We use the official `mcp` Python SDK for MCP client functionality,
not a hand-rolled JSON-RPC + transport implementation.

This reduces MCP client code from ~400 LOC to ~50 LOC and delegates
protocol complexity (JSON-RPC lifecycle, transport negotiation,
tool list change notifications, OAuth) to the well-maintained SDK.

The `mcp` SDK is an optional dependency (`pip install microagent[mcp]`).
