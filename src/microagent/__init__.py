"""MicroAgent — a Python-implemented embeddable AI agent core library."""

from .agent import Agent
from .config import Config
from .core.event import EventBus
from .core.permission import (
    DEFAULT_RULES,
    Decision,
    PermissionDecision,
    PermissionEngine,
    Rule,
    ScriptRule,
)
from .core.store import InMemoryStore, SQLiteStore, Store
from .core.tool import FunctionTool, Tool, ToolRegistry, tool
from .core.types import (
    Event,
    Message,
    SteerEvent,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolProgressDelta,
    ToolResult,
    ToolResultDelta,
    TurnComplete,
    TurnFailed,
    Usage,
)
from .cron.scheduler import CronJob, CronScheduler
from .llm.client import LLMClient, LLMConfig, OpenAIChatClient, StreamDone, StreamEvent
from .mcp.catalog import (
    BUILTIN_MCP_SERVERS,
    MCPServerSpec,
)
from .mcp.catalog import (
    get_server as get_mcp_server,
)
from .mcp.catalog import (
    list_servers as list_mcp_servers,
)
from .mcp.client import connect_mcp_stdio
from .memory.provider import Memory, MemoryProvider, SQLiteMemoryProvider
from .plugin.types import ContextSource, PreLLMHook, ToolHook
from .session.budget import Budget, BudgetExceeded
from .session.runner import SessionRunner
from .skill.curator import Curator
from .skill.loader import ClaudeSkillLoader, CompositeSkillLoader, LoadedSkill, Skill, SkillLoader
from .subagent.manager import DEFAULT_SUBAGENTS, SubagentManager, SubagentSpec
from .terminal.backend import DockerTerminal, LocalTerminal, TerminalBackend, TerminalResult

__version__ = "1.0.0"

__all__ = [
    # Agent
    "Agent",
    # Core types
    "Message",
    "ToolCall",
    "ToolResult",
    "Usage",
    "TextDelta",
    "ToolCallDelta",
    "ToolProgressDelta",
    "ToolResultDelta",
    "TurnComplete",
    "TurnFailed",
    "SteerEvent",
    "Event",
    # Tools
    "Tool",
    "ToolRegistry",
    "FunctionTool",
    "tool",
    # Permission
    "PermissionEngine",
    "PermissionDecision",
    "Rule",
    "Decision",
    "ScriptRule",
    "DEFAULT_RULES",
    # Store
    "Store",
    "SQLiteStore",
    "InMemoryStore",
    # Event
    "EventBus",
    # LLM
    "LLMClient",
    "LLMConfig",
    "OpenAIChatClient",
    "StreamDone",
    "StreamEvent",
    # Config
    "Config",
    # Extension points
    "PreLLMHook",
    "ToolHook",
    "ContextSource",
    # MCP
    "connect_mcp_stdio",
    "MCPServerSpec",
    "BUILTIN_MCP_SERVERS",
    "get_mcp_server",
    "list_mcp_servers",
    # Terminal
    "TerminalResult",
    "TerminalBackend",
    "LocalTerminal",
    "DockerTerminal",
    # Subagent
    "SubagentSpec",
    "SubagentManager",
    "DEFAULT_SUBAGENTS",
    # Skills
    "Skill",
    "LoadedSkill",
    "SkillLoader",
    "ClaudeSkillLoader",
    "CompositeSkillLoader",
    "Curator",
    # Memory
    "Memory",
    "MemoryProvider",
    "SQLiteMemoryProvider",
    # Cron
    "CronScheduler",
    "CronJob",
    # Session
    "Budget",
    "BudgetExceeded",
    "SessionRunner",
]
