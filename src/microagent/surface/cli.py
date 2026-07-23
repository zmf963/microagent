"""CLI: REPL mode + one-shot mode with boxed tool calls and clean text.

Visual hierarchy:
  ╭─ 🔧 tool_name ─────────────────────────╮  ← cyan box for tool call
  │  args                                  │
  ╰─ ✓ result summary ────────────────────╯  ← green/red result line

  Clean text output flows below without markers.
"""

from __future__ import annotations

import re
import shutil
import sys
import time
import unicodedata

from ..agent import Agent
from ..config import Config
from ..core.types import (
    Message, TextDelta, ToolCallDelta, ToolResultDelta, TurnComplete, TurnFailed,
)

# ANSI
GRAY   = "\033[90m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RST    = "\033[0m"


def _term_width() -> int:
    return shutil.get_terminal_size().columns


def _display_width(s: str) -> int:
    """Visible display width accounting for ANSI codes and CJK characters.

    ANSI escape sequences are stripped. CJK characters count as 2.
    Emoji and other wide chars also count as 2.
    """
    # Strip ANSI
    clean = re.sub(r'\033\[[0-9;]*m', '', s)
    w = 0
    for ch in clean:
        ea = unicodedata.east_asian_width(ch)
        if ea in ('W', 'F'):  # Wide / Fullwidth
            w += 2
        else:
            w += 1
    return w


def _pad_to(s: str, target_width: int, fill: str = '─') -> str:
    """Pad s with fill chars to reach target display width."""
    current = _display_width(s)
    return s + fill * max(0, target_width - current)


def main():
    import asyncio
    asyncio.run(_main())


async def _main():
    cli_base_url = None; cli_api_key = None; cli_model = None; cli_system_prompt = None
    positional: list[str] = []

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--base-url" and i + 1 < len(args):
            cli_base_url = args[i + 1]; i += 2
        elif arg == "--api-key" and i + 1 < len(args):
            cli_api_key = args[i + 1]; i += 2
        elif arg == "--model" and i + 1 < len(args):
            cli_model = args[i + 1]; i += 2
        elif arg == "--system-prompt" and i + 1 < len(args):
            cli_system_prompt = args[i + 1]; i += 2
        elif arg in ("--help", "-h"):
            _print_help(); return
        else:
            positional.append(arg); i += 1

    config = Config.from_file(
        cli_base_url=cli_base_url, cli_api_key=cli_api_key,
        cli_model=cli_model, cli_system_prompt=cli_system_prompt,
    )
    if not config.llm.api_key:
        print("Warning: API key not set.", file=sys.stderr)

    # Default: persist sessions to ~/.microagent/sessions.db
    from pathlib import Path as _Path
    from ..core.store import SQLiteStore
    db_path = _Path.home() / ".microagent" / "sessions.db"
    store = SQLiteStore(db_path)
    session_id = f"cli-{int(time.time())}"
    agent = Agent.from_config(
        config.llm, system_prompt=config.system_prompt,
        store=store, session_id=session_id,
    )

    if positional:
        prompt = " ".join(positional)
        _run_streaming(agent, [Message.user(prompt)])
        store.close()
        return

    print(f"{CYAN}{BOLD}MicroAgent v0.1.0{RST}  (model={config.llm.model})")
    print(f"Session: {session_id}")
    print("Commands: /new /list /resume <id>  |  Ctrl-D to exit\n")

    messages: list[Message] = []
    while True:
        try:
            raw = input(f"{BOLD}>>>{RST} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!"); break
        if not raw:
            continue

        # Handle slash commands
        if raw.startswith("/"):
            cmd, *rest = raw[1:].split(maxsplit=1)
            arg = rest[0] if rest else ""

            if cmd == "new":
                session_id = f"cli-{int(time.time())}"
                messages = []
                agent = Agent.from_config(
                    config.llm, system_prompt=config.system_prompt,
                    store=store, session_id=session_id,
                )
                print(f"{GREEN}✓{RST} New session: {session_id}")

            elif cmd == "list":
                sessions = await _list_sessions(store)
                if sessions:
                    print(f"{GRAY}Sessions:{RST}")
                    for sid, count, preview in sessions[-10:]:
                        mark = f"{GREEN}*{RST}" if sid == session_id else " "
                        print(f"  {mark} {GRAY}{sid}{RST} ({count} msgs) {preview}")
                else:
                    print(f"{GRAY}(no saved sessions){RST}")

            elif cmd == "resume":
                target = arg or await _pick_last_session(store)
                if target:
                    history = await store.load_history(target)
                    if history:
                        messages = list(history)
                        session_id = target
                        agent = Agent.from_config(
                            config.llm, system_prompt=config.system_prompt,
                            store=store, session_id=session_id,
                        )
                        print(f"{GREEN}✓{RST} Resumed: {target} ({len(history)} messages)")
                    else:
                        print(f"{RED}✗{RST} Session not found: {target}")
                else:
                    print(f"{RED}✗{RST} No sessions to resume. Use /list to see sessions.")

            elif cmd == "help":
                print(f"{GRAY}/new{RST}       Start a new session")
                print(f"{GRAY}/list{RST}      List saved sessions")
                print(f"{GRAY}/resume{RST}    Resume last session")
                print(f"{GRAY}/resume <id>{RST} Resume a specific session")
                print(f"{GRAY}/help{RST}      Show this help")

            continue

        messages.append(Message.user(raw))
        _run_streaming(agent, messages)
        print()

    store.close()


async def _list_sessions(store) -> list[tuple[str, int, str]]:
    """List sessions with message count and preview."""
    sessions = await store.list_sessions()
    result = []
    for sid in sessions:
        history = await store.load_history(sid)
        count = len(history)
        preview = ""
        if history:
            last = history[-1]
            preview = last.content[:50].replace("\n", " ")
        result.append((sid, count, preview))
    return sorted(result, key=lambda x: x[0])


async def _pick_last_session(store) -> str | None:
    """Pick the most recent session."""
    sessions = await store.list_sessions()
    return sessions[-1] if sessions else None


def _run_streaming(agent: Agent, messages: list[Message]) -> None:
    import asyncio

    async def _stream():
        text_started = False
        thinking_started = False
        pending_tool_call: tuple[str, dict] | None = None

        def _box_width() -> int:
            """Dynamic box width: terminal - 2 margin, capped at 100."""
            return min(_term_width() - 2, 100)

        def _box_line(prefix: str, suffix: str, fill: str = '─') -> str:
            """Build a box border line: prefix + fill + suffix, width-matched."""
            W = _box_width()
            body = _pad_to(prefix, W - _display_width(suffix), fill)
            return f"{GRAY}{body}{suffix}{RST}"

        async for event in agent.runner.run_turn(messages):
            if isinstance(event, TextDelta):
                if event.kind == "thinking":
                    if not thinking_started:
                        thinking_started = True
                        if text_started:
                            print()
                        print(f"{GRAY}────── 💭 thinking ──────{RST}")
                    print(f"{GRAY}{event.text}{RST}", end="", flush=True)

                else:  # kind == "content"
                    if thinking_started and not text_started:
                        print(f"\n{GRAY}──────────────────────────{RST}")
                        thinking_started = False
                    if not text_started:
                        text_started = True
                        if pending_tool_call:
                            print()
                            pending_tool_call = None
                    print(event.text, end="", flush=True)

            elif isinstance(event, ToolCallDelta):
                title = f"╭─ 🔧 {event.name} "
                print(f"\n{_box_line(title, '╮')}")
                args = _short_args(event.arguments)
                W = _box_width()
                arg_body = f"│ {GRAY}{args}{RST}"
                arg_pad = W - _display_width(arg_body) - 1
                print(f"{GRAY}{arg_body}{' ' * max(0, arg_pad)}│{RST}")
                pending_tool_call = (event.name, event.arguments)

            elif isinstance(event, ToolResultDelta):
                summary = _summarize(event.content)
                mark = f"{RED}✗{RST}" if event.is_error else f"{GREEN}✓{RST}"
                print(_box_line(f"╰─ {mark} {GRAY}{summary}{RST} ", "╯"))
                pending_tool_call = None

            elif isinstance(event, TurnComplete):
                if pending_tool_call:
                    print()
                if not text_started:
                    print(event.content)
                print()
                return

            elif isinstance(event, TurnFailed):
                if pending_tool_call:
                    print()
                print(f"{RED}✗{RST} {event.reason}")
                return

    asyncio.run(_stream())


def _short_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
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
    print("Usage: microagent [options] [prompt]")
    print()
    print("Options:")
    print("  --base-url URL        LLM API base URL")
    print("  --api-key KEY         API key")
    print("  --model MODEL         Model name")
    print("  --system-prompt TEXT  System prompt")
    print("  --help, -h            Show this help")
    print()
    print("Config file: ~/.microagent/config.yaml")
    print("Env vars: MICROAGENT_BASE_URL, MICROAGENT_API_KEY, MICROAGENT_MODEL")
