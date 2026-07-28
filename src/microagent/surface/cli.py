"""CLI: REPL mode + one-shot mode with Rich UI.

Visual hierarchy:
  ╭─ 🔧 tool_name ─────────────────────────╮  ← cyan Panel for tool call
  │  args                                  │
  ╰─ ✓ result summary ────────────────────╯  ← green/red result line

  Clean text output flows below without markers.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.theme import Theme

from ..agent import Agent
from ..config import Config
from ..core.types import (
    Message,
    TextDelta,
    ToolCallDelta,
    ToolProgressDelta,
    ToolResultDelta,
    TurnComplete,
    TurnFailed,
    Usage,
)

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

CLI_THEME = Theme({
    "info": "cyan",
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "dim": "grey62",
    "thinking": "grey70 italic",
    "tool.title": "bold cyan",
    "tool.args": "grey62",
    "tool.result.ok": "green",
    "tool.result.error": "red",
    "prompt": "bold",
    "status.tokens": "grey62",
    "status.cost": "grey62",
})

console = Console(theme=CLI_THEME, highlight=False)


# ---------------------------------------------------------------------------
# Usage tracker
# ---------------------------------------------------------------------------


class _UsageTracker:
    """Tracks cumulative token usage across turns (CLI local, not in core)."""

    def __init__(self):
        self.total_input = 0
        self.total_output = 0
        self.total_cost = 0.0
        self.turns = 0

    def record(self, usage: Usage) -> None:
        self.total_input += usage.input_tokens
        self.total_output += usage.output_tokens
        self.total_cost += usage.cost_usd
        self.turns += 1

    def reset(self) -> None:
        self.total_input = 0
        self.total_output = 0
        self.total_cost = 0.0
        self.turns = 0

    def summary(self) -> str:
        return (
            f"Tokens: {self.total_input} in / {self.total_output} out, "
            f"Cost: ${self.total_cost:.4f}, Turns: {self.turns}"
        )

    def status_table(self) -> Table:
        """Rich Table for the bottom status bar."""
        t = Table.grid(expand=True)
        t.add_column(justify="left")
        t.add_column(justify="right")
        t.add_row(
            f"[status.tokens]tokens: {self.total_input + self.total_output}[/]",
            f"[status.cost]cost: ${self.total_cost:.4f} | turns: {self.turns}[/]",
        )
        return t


@dataclass
class ReplState:
    """Mutable state shared across CLI command handlers."""

    agent: Agent
    config: Config
    store: object
    session_id: str
    messages: list[Message] = field(default_factory=list)
    usage_tracker: _UsageTracker = field(default_factory=_UsageTracker)
    disabled_skills: set[str] = field(default_factory=set)


def main():
    import asyncio

    asyncio.run(_main())


# ---------------------------------------------------------------------------
# Readline: history + slash-command completion (stdlib, zero deps)
# ---------------------------------------------------------------------------

_HISTORY_FILE = "~/.microagent/cli_history"


def _setup_readline() -> None:
    """Enable line editing, persistent history, and /-command completion.

    Skipped silently on platforms without readline (e.g. Windows).
    """
    import atexit
    import os

    try:
        import readline
    except ImportError:
        return  # Windows: no readline — history/completion unavailable

    hist_path = os.path.expanduser(_HISTORY_FILE)
    os.makedirs(os.path.dirname(hist_path), exist_ok=True)
    try:
        readline.read_history_file(hist_path)
    except (FileNotFoundError, PermissionError, OSError):
        pass
    readline.set_history_length(1000)

    def _write_history() -> None:
        try:
            readline.write_history_file(hist_path)
        except OSError:
            pass

    atexit.register(_write_history)

    def _completer(text: str, state: int) -> str | None:
        # Only complete when the line is a slash command (starts with /)
        buf = readline.get_line_buffer()
        if buf.startswith("/"):
            options = [f"/{name} " for name in _COMMANDS if name.startswith(text)]
        else:
            options = []
        try:
            return options[state]
        except IndexError:
            return None

    readline.set_completer(_completer)
    readline.parse_and_bind("tab: complete")


async def _main():
    cli_base_url = None
    cli_api_key = None
    cli_model = None
    cli_system_prompt = None
    positional: list[str] = []

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--base-url" and i + 1 < len(args):
            cli_base_url = args[i + 1]
            i += 2
        elif arg == "--api-key" and i + 1 < len(args):
            cli_api_key = args[i + 1]
            i += 2
        elif arg == "--model" and i + 1 < len(args):
            cli_model = args[i + 1]
            i += 2
        elif arg == "--system-prompt" and i + 1 < len(args):
            cli_system_prompt = args[i + 1]
            i += 2
        elif arg in ("--help", "-h"):
            _print_help()
            return
        else:
            positional.append(arg)
            i += 1

    config = Config.from_file(
        cli_base_url=cli_base_url,
        cli_api_key=cli_api_key,
        cli_model=cli_model,
        cli_system_prompt=cli_system_prompt,
    )
    if not config.llm.api_key:
        console.print("[warning]Warning: API key not set.[/]")

    from pathlib import Path as _Path

    from ..core.store import SQLiteStore

    db_path = _Path.home() / ".microagent" / "sessions.db"
    store = SQLiteStore(db_path)
    session_id = f"cli-{int(time.time())}"
    agent = Agent.from_config(
        config.llm,
        system_prompt=config.system_prompt,
        store=store,
        session_id=session_id,
        skills_path=config.skills_path,
    )

    if positional:
        prompt = " ".join(positional)
        await _run_streaming(agent, [Message.user(prompt)])
        await agent.close()
        store.close()
        return

    console.print(f"[info]MicroAgent v1.0.0[/]  (model={config.llm.model})")
    console.print(f"Session: {session_id}")
    console.print("Commands: /new /list /resume /compact /model /history /skill /clear /cost /plan /build | Tab completes /commands | Ctrl-D to exit\n")

    _setup_readline()

    messages: list[Message] = []
    usage_tracker = _UsageTracker()
    disabled_skills: set[str] = set()

    repl_state = ReplState(
        agent=agent,
        config=config,
        store=store,
        session_id=session_id,
        messages=messages,
        usage_tracker=usage_tracker,
        disabled_skills=disabled_skills,
    )

    while True:
        try:
            raw = Prompt.ask("[prompt]>>>[/prompt]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break
        if not raw:
            continue

        if raw.startswith("/"):
            cmd, *rest = raw[1:].split(maxsplit=1)
            arg = rest[0] if rest else ""

            handler_entry = _COMMANDS.get(cmd)
            if handler_entry:
                handler, _desc = handler_entry
                await handler(repl_state, arg)
            else:
                console.print(f"[error]✗[/] Unknown command: /{cmd}. Type /help for available commands.")

            continue

        messages.append(Message.user(raw))
        await _run_streaming(agent, messages, usage_tracker)
        console.print()

    await agent.close()
    store.close()


async def _list_sessions(store) -> list[tuple[str, int, str]]:
    """List sessions with message count and preview."""
    summaries = await store.session_summaries()
    return [(s["session_id"], s["count"], s["preview"]) for s in summaries]


async def _pick_last_session(store) -> str | None:
    """Pick the most recent session."""
    sessions = await store.list_sessions()
    return sessions[0] if sessions else None


async def _run_streaming(agent: Agent, messages: list[Message], usage_tracker: _UsageTracker | None = None) -> None:
    """Run agent turn with Rich streaming output."""

    async def _stream():
        text_started = False
        thinking_started = False
        pending_tool_call: tuple[str, dict] | None = None

        async for event in agent.runner.run_turn(messages):
            if isinstance(event, Usage):
                if usage_tracker is not None:
                    usage_tracker.record(event)

            elif isinstance(event, TextDelta):
                if event.kind == "thinking":
                    if not thinking_started:
                        thinking_started = True
                        if text_started:
                            console.print()
                        console.rule("[thinking]💭 thinking[/]", style="dim")
                    console.print(f"[thinking]{event.text}[/]", end="", highlight=False)

                else:  # kind == "content"
                    if thinking_started and not text_started:
                        console.print()
                        console.rule(style="dim")
                        thinking_started = False
                    if not text_started:
                        text_started = True
                        if pending_tool_call:
                            console.print()
                            pending_tool_call = None
                    console.print(event.text, end="", highlight=False)

            elif isinstance(event, ToolCallDelta):
                args = _short_args(event.arguments)
                panel = Panel(
                    f"[tool.args]{args}[/]",
                    title=f"[tool.title]🔧 {event.name}[/]",
                    border_style="cyan",
                    padding=(0, 1),
                )
                console.print()
                console.print(panel)
                pending_tool_call = (event.name, event.arguments)

            elif isinstance(event, ToolResultDelta):
                summary = _summarize(event.content)
                mark = "[tool.result.error]✗[/]" if event.is_error else "[tool.result.ok]✓[/]"
                console.print(f"{mark} [dim]{summary}[/]")
                pending_tool_call = None

            elif isinstance(event, ToolProgressDelta):
                text = event.text or ""
                for line in text.splitlines():
                    console.print(f" [dim]┊[/] {line}")

            elif isinstance(event, TurnComplete):
                if pending_tool_call:
                    console.print()
                if not text_started:
                    console.print(Markdown(event.content))
                # Status line after each turn: tokens + cost
                if usage_tracker is not None:
                    console.print(usage_tracker.status_table())
                else:
                    console.print()
                return

            elif isinstance(event, TurnFailed):
                if pending_tool_call:
                    console.print()
                console.print(f"[error]✗[/] {event.reason}")
                return

    await _stream()


def _short_args(args: dict) -> str:
    parts = []
    for _k, v in args.items():
        s = str(v)
        if " " in s:
            s = f'"{s}"'
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(s)
    return " ".join(parts)


def _summarize(content: str) -> str:
    clean = content.replace("\n", " ").strip()
    if len(clean) > 70:
        clean = clean[:67] + "..."
    return clean


def _print_help():
    console.print("Usage: microagent [options] [prompt]")
    console.print()
    console.print("Options:")
    console.print("  --base-url URL        LLM API base URL")
    console.print("  --api-key KEY         API key")
    console.print("  --model MODEL         Model name")
    console.print("  --system-prompt TEXT  System prompt")
    console.print("  --help, -h            Show this help")
    console.print()
    console.print("Config file: ~/.microagent/config.yaml")
    console.print("Env vars: MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL")


# ---------------------------------------------------------------------------
# Slash command handlers
# ---------------------------------------------------------------------------


async def _cmd_new(state: ReplState, arg: str) -> None:
    agent = state.agent
    await agent.close()
    config = state.config
    store = state.store
    state.session_id = f"cli-{int(time.time())}"
    state.messages = []
    state.usage_tracker.reset()
    state.disabled_skills.clear()
    state.agent = Agent.from_config(
        config.llm,
        system_prompt=config.system_prompt,
        store=store,
        session_id=state.session_id,
        skills_path=config.skills_path,
    )
    console.print(f"[success]✓[/] New session: {state.session_id}")


async def _cmd_list(state: ReplState, arg: str) -> None:
    sessions = await _list_sessions(state.store)
    if sessions:
        table = Table(title="Sessions", show_header=True, header_style="bold cyan")
        table.add_column("", width=2)
        table.add_column("Session ID", style="dim")
        table.add_column("Msgs", justify="right")
        table.add_column("Preview", overflow="fold")
        for sid, count, preview in sessions[:10]:
            mark = "[success]*[/]" if sid == state.session_id else " "
            table.add_row(mark, sid, str(count), preview)
        console.print(table)
    else:
        console.print("[dim](no saved sessions)[/]")


async def _cmd_resume(state: ReplState, arg: str) -> None:
    target = arg or await _pick_last_session(state.store)
    if target:
        history = await state.store.load_history(target)
        if history:
            await state.agent.close()
            state.messages = list(history)
            state.session_id = target
            state.usage_tracker.reset()
            config = state.config
            store = state.store
            state.agent = Agent.from_config(
                config.llm,
                system_prompt=config.system_prompt,
                store=store,
                session_id=target,
                skills_path=config.skills_path,
            )
            console.print(f"[success]✓[/] Resumed: {target} ({len(history)} messages)")
        else:
            console.print(f"[error]✗[/] Session not found: {target}")
    else:
        console.print(f"[error]✗[/] No sessions to resume. Use /list to see sessions.")


async def _cmd_compact(state: ReplState, arg: str) -> None:
    messages = state.messages
    if len(messages) < 5:
        console.print("[dim](not enough messages to compact)[/]")
        return

    from ..session.compress import (
        CompactionState,
        compact_conversation,
        count_tokens,
    )

    before_count = len(messages)
    before_tokens = count_tokens(tuple(messages))
    agent = state.agent
    state_obj = getattr(agent.runner, "_compaction_state", CompactionState())
    compressed = await compact_conversation(
        tuple(messages),
        agent.runner.llm,
        context_window=before_tokens + 8000,
        state=state_obj,
        force=True,
    )
    messages[:] = list(compressed)
    await agent.close()
    config = state.config
    store = state.store
    state.agent = Agent.from_config(
        config.llm,
        system_prompt=config.system_prompt,
        store=store,
        session_id=state.session_id,
        skills_path=config.skills_path,
    )
    state.agent.runner._compaction_state = state_obj
    after_count = len(messages)
    after_tokens = count_tokens(tuple(messages))
    console.print(
        f"[success]✓[/] Compacted: {before_count} → {after_count} messages, "
        f"{before_tokens} → {after_tokens} tokens"
    )


async def _cmd_model(state: ReplState, arg: str) -> None:
    config = state.config
    if not arg:
        console.print(f"Current model: {config.llm.model}")
        return
    from ..llm.client import LLMConfig, OpenAIChatClient

    new_llm_config = LLMConfig(
        base_url=config.llm.base_url,
        api_key=config.llm.api_key,
        model=arg,
        reasoning_effort=config.llm.reasoning_effort,
        service_tier=config.llm.service_tier,
        auxiliary_model=config.llm.auxiliary_model,
    )
    old_llm = state.agent.runner.llm
    if hasattr(old_llm, "close"):
        await old_llm.close()
    state.agent.runner.llm = OpenAIChatClient(new_llm_config)
    console.print(f"[success]✓[/] Model switched to: {arg}")


async def _cmd_history(state: ReplState, arg: str) -> None:
    messages = state.messages
    if not messages:
        console.print("[dim](no messages in this session)[/]")
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim", width=4)
    table.add_column("Role", width=10)
    table.add_column("Content", overflow="fold")
    for i, msg in enumerate(messages):
        preview = msg.content[:80].replace("\n", " ")
        if len(msg.content) > 80:
            preview += "..."
        table.add_row(str(i), msg.role, preview)
    console.print(table)


async def _cmd_skill(state: ReplState, arg: str) -> None:
    parts = arg.split(maxsplit=1) if arg else []
    subcmd = parts[0] if parts else "list"
    skill_name = parts[1] if len(parts) > 1 else ""

    disabled = state.disabled_skills

    if subcmd == "list":
        agent = state.agent
        loader = agent.runner.skill_loader
        if loader is None:
            console.print("[dim](no skill loader configured)[/]")
            return
        try:
            skills = await loader.load()
        except Exception:
            console.print("[dim](failed to load skills)[/]")
            return
        if not skills:
            console.print("[dim](no skills found)[/]")
            return
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Status", width=10)
        table.add_column("Skill", style="dim")
        table.add_column("Description", overflow="fold")
        for s in skills:
            status = "[error]disabled[/]" if s.name in disabled else "[success]enabled[/]"
            desc = s.description[:60] if s.description else ""
            table.add_row(status, f"{s.namespace}:{s.name}", desc)
        console.print(table)

    elif subcmd == "unload":
        if not skill_name:
            console.print("[error]✗[/] Usage: /skill unload <name>")
            return
        disabled.add(skill_name)
        console.print(f"[success]✓[/] Skill '{skill_name}' disabled (will be filtered from matches)")

    elif subcmd == "load":
        if not skill_name:
            console.print("[error]✗[/] Usage: /skill load <name>")
            return
        if skill_name in disabled:
            disabled.discard(skill_name)
            console.print(f"[success]✓[/] Skill '{skill_name}' re-enabled")
        else:
            console.print(f"[dim]Skill '{skill_name}' is already enabled[/]")

    else:
        console.print(f"[error]✗[/] Unknown subcommand: {subcmd}. Use: list, load, unload")


async def _cmd_clear(state: ReplState, arg: str) -> None:
    console.clear()


async def _cmd_cost(state: ReplState, arg: str) -> None:
    tracker = state.usage_tracker
    console.print(tracker.summary())


async def _cmd_help(state: ReplState, arg: str) -> None:
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Command", style="dim")
    table.add_column("Description")
    for name, (_handler, desc) in sorted(_COMMANDS.items()):
        table.add_row(f"/{name}", desc)
    console.print(table)


async def _cmd_plan(state: ReplState, arg: str) -> None:
    state.agent.runner.mode = "plan"
    console.print("[success]✓[/] Switched to plan mode (read-only tools)")


async def _cmd_build(state: ReplState, arg: str) -> None:
    state.agent.runner.mode = "build"
    console.print("[success]✓[/] Switched to build mode (all tools enabled)")


# Command registry: name → (handler, description)
_COMMANDS: dict[str, tuple] = {
    "new": (_cmd_new, "Start a new session"),
    "list": (_cmd_list, "List saved sessions"),
    "resume": (_cmd_resume, "Resume last or specific session"),
    "compact": (_cmd_compact, "Manually compress current conversation"),
    "model": (_cmd_model, "Show or switch model (/model <name>)"),
    "history": (_cmd_history, "Show message history for current session"),
    "skill": (_cmd_skill, "Manage skills (/skill list|load|unload)"),
    "clear": (_cmd_clear, "Clear the screen"),
    "cost": (_cmd_cost, "Show token usage and cost for this session"),
    "plan": (_cmd_plan, "Switch to plan mode (read-only tools)"),
    "build": (_cmd_build, "Switch to build mode (all tools)"),
    "help": (_cmd_help, "Show available commands"),
}
