"""MicroAgent — a Python-implemented embeddable AI agent core library."""

from .core.types import (
    Message, ToolCall, ToolResult, Usage,
    TextDelta, ToolCallDelta, TurnComplete, TurnFailed, Event,
)
from .core.tool import Tool, ToolRegistry, FunctionTool, tool
from .core.permission import PermissionEngine, Rule, Decision, ScriptRule, DEFAULT_RULES
from .core.store import Store, SQLiteStore, InMemoryStore
from .core.event import EventBus
from .llm.client import LLMClient, LLMConfig, OpenAIChatClient, StreamDone, StreamEvent
from .plugin.types import PreLLMHook, ToolHook, ContextSource
from .mcp.client import connect_mcp_stdio
from .terminal.backend import TerminalResult, TerminalBackend, LocalTerminal, DockerTerminal
from .subagent.manager import SubagentSpec, SubagentManager, DEFAULT_SUBAGENTS
from .skill.loader import Skill, LoadedSkill, SkillLoader, ClaudeSkillLoader, CompositeSkillLoader
from .skill.curator import Curator, SkillUsage
from .memory.provider import Memory, MemoryProvider, SQLiteMemoryProvider
from .cron.scheduler import CronScheduler, CronJob
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
    "PermissionEngine", "Rule", "Decision", "ScriptRule", "DEFAULT_RULES",
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
    # Terminal
    "TerminalResult", "TerminalBackend", "LocalTerminal", "DockerTerminal",
    # Subagent
    "SubagentSpec", "SubagentManager", "DEFAULT_SUBAGENTS",
    # Skills
    "Skill", "LoadedSkill", "SkillLoader", "ClaudeSkillLoader", "CompositeSkillLoader",
    "Curator", "SkillUsage",
    # Memory
    "Memory", "MemoryProvider", "SQLiteMemoryProvider",
    # Cron
    "CronScheduler", "CronJob",
    # Session
    "Budget", "BudgetExceeded", "SessionRunner",
]
