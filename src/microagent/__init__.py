"""MicroAgent — a Python-implemented embeddable AI agent core library."""

from .core.types import (
    Message, ToolCall, ToolResult, Usage,
    TextDelta, ToolCallDelta, TurnComplete, TurnFailed, Event,
)
from .core.tool import Tool, ToolRegistry, FunctionTool, tool
from .llm.client import LLMClient, LLMConfig, OpenAIChatClient, StreamDone, StreamEvent
from .session.budget import Budget
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
    # LLM
    "LLMClient", "LLMConfig", "OpenAIChatClient", "StreamDone", "StreamEvent",
    # Session
    "Budget", "SessionRunner",
]
