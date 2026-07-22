"""MicroAgent — a Python-implemented embeddable AI agent core library."""

from .core.types import (
    Message, ToolCall, ToolResult, Usage,
    TextDelta, ToolCallDelta, TurnComplete, TurnFailed, Event,
)
from .core.tool import Tool, ToolRegistry, FunctionTool, tool
from .core.permission import PermissionEngine, Rule, Decision, DEFAULT_RULES
from .core.store import Store, SQLiteStore, InMemoryStore
from .core.event import EventBus
from .llm.client import LLMClient, LLMConfig, OpenAIChatClient, StreamDone, StreamEvent
from .plugin.types import PreLLMHook, ToolHook, ContextSource
from .mcp.client import connect_mcp_stdio
from .subagent.manager import SubagentSpec, SubagentManager, DEFAULT_SUBAGENTS
from .session.budget import Budget, BudgetExceeded
from .session.runner import SessionRunner
from .agent import Agent

__version__ = "0.1.0"

__all__ = [
    # Agent
    "Agent",
    # Core types
    "Message", "ToolCall", "ToolResult", "Usage",
    "TextDelta", "ToolCallDelta", "TurnComplete", "TurnFailed", "Event",
    # Tools
    "Tool", "ToolRegistry", "FunctionTool", "tool",
    # Permission
    "PermissionEngine", "Rule", "Decision", "DEFAULT_RULES",
    # Store
    "Store", "SQLiteStore", "InMemoryStore",
    # Event
    "EventBus",
    # LLM
    "LLMClient", "LLMConfig", "OpenAIChatClient", "StreamDone", "StreamEvent",
    # Extension points
    "PreLLMHook", "ToolHook", "ContextSource",
    # MCP
    "connect_mcp_stdio",
    # Subagent
    "SubagentSpec", "SubagentManager", "DEFAULT_SUBAGENTS",
    # Session
    "Budget", "BudgetExceeded", "SessionRunner",
]
