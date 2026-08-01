"""CLI: REPL mode + one-shot mode with Rich UI.

Visual hierarchy:
  ╭─ 🔧 tool_name ─────────────────────────╮  ← cyan Panel for tool call
  │  args                                  │
  ╰─ ✓ result summary ────────────────────╯  ← green/red result line

  Clean text output flows below without markers.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as _rich_escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
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

    def status_line(self) -> str:
        """One-line status string, printed on its own line after LLM output."""
        return (
            "[dim]📊[/] "
            f"[status.tokens]tokens: {self.total_input + self.total_output}[/status.tokens]  "
            "[dim]💰[/] "
            f"[status.cost]cost: ${self.total_cost:.4f}[/status.cost]  "
            f"[dim]🔄 turns: {self.turns}[/dim]"
        )


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
    show_thinking: bool = False


def main():
    import asyncio

    asyncio.run(_main())


def _resolve_show_thinking(cli_flag: bool | None) -> bool:
    """Resolve whether to display reasoning/thinking deltas.

    Priority: CLI flag > MICROAGENT_SHOW_THINKING env > config file
    ``display.show_thinking`` > default (False). Display-only concern, kept in
    the surface layer so the core ``Config``/Agent stay unaware of it.
    """
    if cli_flag is not None:
        return cli_flag
    env_val = os.environ.get("MICROAGENT_SHOW_THINKING")
    if env_val is not None:
        return env_val.strip().lower() in {"1", "true", "yes", "on"}
    # Config file: ~/.microagent/config.yaml → display.show_thinking
    try:
        path = Config._config_path()
        if path.exists():
            import yaml

            data = yaml.safe_load(path.read_text()) or {}
            display = data.get("display", {}) if isinstance(data, dict) else {}
            if isinstance(display, dict):
                return bool(display.get("show_thinking", False))
    except Exception:
        pass
    return False


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
    cli_show_thinking: bool | None = None
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
        elif arg == "--show-thinking":
            cli_show_thinking = True
            i += 1
        elif arg == "--no-show-thinking":
            cli_show_thinking = False
            i += 1
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

    _show_thinking = _resolve_show_thinking(cli_show_thinking)

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
        tracker = _UsageTracker()
        await _run_streaming(
            agent, [Message.user(prompt)], tracker, show_thinking=_show_thinking
        )
        await agent.close()
        store.close()
        return

    console.print(f"[info]MicroAgent v1.0.0[/]  (model={config.llm.model})")
    console.print(f"Session: {session_id}")
    console.print("Commands: /new /list /resume /compact /model /history /skill /clear /cost /plan /build /thinking | Tab completes /commands | Esc×2 to interrupt | Ctrl-D to exit\n")

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
        show_thinking=_show_thinking,
    )

    while True:
        try:
            # Multi-line: paste code blocks with triple backticks, then Enter on empty line
            raw = _read_multiline()
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

        repl_state.messages.append(Message.user(raw))
        await _run_streaming(
            repl_state.agent,
            repl_state.messages,
            repl_state.usage_tracker,
            show_thinking=repl_state.show_thinking,
        )
        console.print()

    await repl_state.agent.close()
    store.close()


async def _list_sessions(store) -> list[tuple[str, int, str]]:
    """List sessions with message count and preview."""
    summaries = await store.session_summaries()
    return [(s["session_id"], s["count"], s["preview"]) for s in summaries]


def _read_multiline() -> str:
    """Read user input with multi-line support.

    - Single line: normal input
    - Multi-line: paste text containing newlines, then press Enter on empty line
    - Code blocks: paste ```...``` blocks directly
    """
    first = Prompt.ask("[prompt]>>>[/prompt]").strip()
    if not first:
        return ""

    # If first line ends with ``` or contains newline continuation, read more
    if first.endswith("```") or first.endswith("\\"):
        lines = [first]
        while True:
            try:
                line = Prompt.ask("[dim]...[/dim]").rstrip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            lines.append(line)
        return "\n".join(lines)

    return first


async def _pick_last_session(store) -> str | None:
    """Pick the most recent session."""
    sessions = await store.list_sessions()
    return sessions[0] if sessions else None


async def _run_streaming(
    agent: Agent,
    messages: list[Message],
    usage_tracker: _UsageTracker | None = None,
    *,
    show_thinking: bool = False,
) -> None:
    """Run agent turn — OpenCode style: spinner + final Markdown render.

    Flow:
      1. spinner "Thinking…" while waiting
      2. tool call → cyan Panel, spinner restarts "Running…"
      3. tool result → green/red Panel
      4. LLM text collected silently (no raw stream print)
      5. TurnComplete → spinner stops, final text rendered as Markdown+Syntax
      6. status line (📊 tokens 💰 cost 🔄 turns)

    When ``show_thinking`` is True, reasoning deltas are printed inline
    under a dim "thinking" rule.
    """
    import asyncio
    import sys

    try:
        import termios
        import tty
        _HAS_TERMIOS = True
    except ImportError:
        _HAS_TERMIOS = False

    _esc_count = 0
    _interrupt = asyncio.Event()

    async def _watch_esc() -> None:
        nonlocal _esc_count
        if not sys.stdin.isatty() or not _HAS_TERMIOS:
            return
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            # Use setcbreak, NOT setraw: setraw disables OPOST (output
            # post-processing), which kills ONLCR (\n → \r\n). In raw mode
            # every Rich newline moves the cursor down WITHOUT returning to
            # column 0, so multi-line Panels/Markdown/progress lines overlap
            # and misalign. setcbreak keeps output processing intact while
            # still disabling ECHO/ICANON for single-key Esc detection.
            tty.setcbreak(fd)
            while not _interrupt.is_set():
                ch = await asyncio.to_thread(sys.stdin.read, 1)
                if ch == "\x1b":
                    _esc_count += 1
                    if _esc_count >= 2:
                        _interrupt.set()
                        return
                    await asyncio.sleep(0.5)
                    _esc_count = 0
                else:
                    _esc_count = 0
        except (OSError, termios.error):
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    _esc_task = asyncio.create_task(_watch_esc())
    _status = console.status("[dim]⠋ Thinking…[/]", spinner="dots")
    _status.start()

    async def _stream():
        nonlocal _status
        text_buffer: list[str] = []
        pending_tool: tuple[str, dict] | None = None
        thinking_started = False

        async for event in agent.runner.run_turn(messages):
            if _interrupt.is_set():
                agent.runner.interrupt()
                console.print("\n[warning]⚠ Interrupted by user (Esc×2)[/]")
                return

            if isinstance(event, Usage):
                if usage_tracker is not None:
                    usage_tracker.record(event)

            elif isinstance(event, TextDelta):
                if event.kind == "thinking":
                    if show_thinking:
                        if _status:
                            _status.stop()
                            _status = None
                        if not thinking_started:
                            thinking_started = True
                            console.rule("[thinking]💭 thinking[/]", style="dim")
                        console.print(f"[thinking]{_rich_escape(event.text)}[/]", end="", highlight=False)
                else:  # content
                    text_buffer.append(event.text)
                    if _status:
                        _status.stop()
                        _status = None

            elif isinstance(event, ToolCallDelta):
                if _status:
                    _status.stop()
                    _status = None
                args = _short_args(event.arguments)
                console.print(Panel(
                    f"[tool.args]{_rich_escape(args)}[/]",
                    title=f"[tool.title]🔧 {_rich_escape(event.name)}[/]",
                    title_align="left",
                    border_style="cyan",
                    padding=(0, 1),
                    expand=False,
                ))
                pending_tool = (event.name, event.arguments)
                _status = console.status("[dim]⠋ Running…[/]", spinner="dots")
                _status.start()

            elif isinstance(event, ToolResultDelta):
                if _status:
                    _status.stop()
                    _status = None
                summary = _summarize(event.content)
                mark = "[tool.result.error]✗[/]" if event.is_error else "[tool.result.ok]✓[/]"
                border = "red" if event.is_error else "green"
                console.print(Panel(
                    f"{mark} [dim]{_rich_escape(summary)}[/]",
                    border_style=border,
                    padding=(0, 1),
                    expand=False,
                ))
                pending_tool = None

            elif isinstance(event, ToolProgressDelta):
                if _status:
                    _status.stop()
                    _status = None
                for line in (event.text or "").splitlines():
                    console.print(f" [dim]┊[/] {_rich_escape(line)}")

            elif isinstance(event, TurnComplete):
                if _status:
                    _status.stop()
                    _status = None
                # Final render: Markdown + Syntax highlighting
                full = "".join(text_buffer) if text_buffer else event.content
                if full.strip():
                    console.print()
                    _render_content(full)
                # Status line
                if usage_tracker is not None:
                    console.print()
                    console.print(usage_tracker.status_line())
                else:
                    console.print()
                return

            elif isinstance(event, TurnFailed):
                if _status:
                    _status.stop()
                    _status = None
                console.print(f"[error]✗[/] {_rich_escape(str(event.reason))}")
                return

    try:
        await _stream()
    finally:
        if _status:
            _status.stop()
        _interrupt.set()
        if _esc_task and not _esc_task.done():
            _esc_task.cancel()
            try:
                await _esc_task
            except asyncio.CancelledError:
                pass


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


def _render_content(content: str) -> None:
    """Render LLM content with Markdown + Syntax highlighting for code blocks."""
    if "```" in content:
        parts = content.split("```")
        for i, part in enumerate(parts):
            if i % 2 == 0:
                # Plain text / markdown
                if part.strip():
                    console.print(Markdown(part))
            else:
                # Code block: first line is language, rest is code
                lines = part.split("\n")
                if lines:
                    lang = lines[0].strip() or "text"
                    code = "\n".join(lines[1:])
                    if code.strip():
                        console.print(Syntax(code, lang, theme="monokai", line_numbers=True))
    else:
        console.print(Markdown(content))


def _print_help():
    console.print("Usage: microagent [options] [prompt]")
    console.print()
    console.print("Options:")
    console.print("  --base-url URL        LLM API base URL")
    console.print("  --api-key KEY         API key")
    console.print("  --model MODEL         Model name")
    console.print("  --system-prompt TEXT  System prompt")
    console.print("  --show-thinking       Show reasoning/thinking deltas (default: hidden)")
    console.print("  --no-show-thinking    Hide reasoning/thinking deltas")
    console.print("  --help, -h            Show this help")
    console.print()
    console.print("Config file: ~/.microagent/config.yaml")
    console.print("Env vars: MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL, MICROAGENT_SHOW_THINKING")


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
        console.print("[error]✗[/] No sessions to resume. Use /list to see sessions.")


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


async def _cmd_thinking(state: ReplState, arg: str) -> None:
    """Toggle or set display of reasoning (💭 thinking) deltas."""
    val = arg.strip().lower()
    if val in {"on", "true", "yes", "1"}:
        state.show_thinking = True
    elif val in {"off", "false", "no", "0"}:
        state.show_thinking = False
    else:
        state.show_thinking = not state.show_thinking
    status = "shown" if state.show_thinking else "hidden"
    console.print(f"[success]✓[/] Reasoning/thinking deltas are now {status}")


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
    "thinking": (_cmd_thinking, "Toggle reasoning display (/thinking [on|off])"),
    "help": (_cmd_help, "Show available commands"),
}
