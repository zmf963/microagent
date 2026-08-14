"""SessionRunner — the core conversation loop.

Consumes an LLMClient and a ToolRegistry, runs the tool-use loop:

    while not budget.exhausted:
        1. Call LLM (stream)
        2. If tool_calls → execute → append results → loop
        3. If text → yield TurnComplete → return

Supports streaming tool output: tools that return AsyncIterator[str]
yield ToolProgressDelta events for real-time display.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from ..core.permission import PermissionEngine

import anyio

from ..core.event import EventBus
from ..core.store import Store
from ..core.tool import ToolRegistry
from ..core.types import (
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
from ..llm.client import LLMClient, StreamDone
from .budget import Budget, BudgetExceeded

logger = logging.getLogger(__name__)

# Tools whose output is large by design (base64-encoded media) and must
# reach the model verbatim. Routing them through ToolOutputStore would
# replace the data URL with a head/tail preview — a typical full-page PNG
# screenshot is 100-500 KB, well past the 50 KB gate, and the truncation
# is silent, leaving the vision model with a corrupt image.
_OUTPUT_STORE_EXEMPT = frozenset({"browser_vision", "vision_analyze"})


class SessionRunner:
    """Core loop: LLM → tool calls → LLM → ... → text response."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        budget: Budget | None = None,
        system_prompt: str = "",
        store: Store | None = None,
        session_id: str = "default",
        event_bus: EventBus | None = None,
        pre_llm_hooks: tuple = (),
        tool_hooks: tuple = (),
        context_sources: tuple = (),
        skill_loader: object = None,
        memory: object = None,
        compression_threshold: int = 0,
        permission_engine: "PermissionEngine | None" = None,
    ):
        self.llm = llm
        self.registry = registry
        self.budget = budget or Budget()
        self.system_prompt = system_prompt
        self.store = store
        self.session_id = session_id
        self.event_bus = event_bus
        self.pre_llm_hooks = pre_llm_hooks
        self.tool_hooks = tool_hooks
        self.context_sources = context_sources
        self.skill_loader = skill_loader
        self.memory = memory
        self.compression_threshold = compression_threshold
        self.permission_engine = permission_engine

        self._cached_system: str | None = None
        self._cached_tools: list[dict] | None = None
        self._cached_tools_version: int = -1  # registry.version at last rebuild
        self._cached_skill_catalog: str = ""  # stable part of system prompt
        # OrderedDict preserves insertion order + supports move_to_end/popitem
        # for LRU eviction. Replaces the prior hand-rolled set+list pair.
        self._loaded_skills: OrderedDict[str, None] = OrderedDict()
        self._max_loaded_skills: int = 10  # cap to prevent unbounded growth
        # Skill names disabled at runtime (CLI /skill unload). The CLI's
        # ReplState.disabled_skills was a dead write — nothing consumed it,
        # so unloaded skills kept being matched and injected every turn.
        # The runner is the consumer; matching and body injection both
        # filter on this set.
        self.disabled_skills: set[str] = set()
        self._cached_mode: str = "build"
        # Compaction state — initialized here (not lazily in run_turn) so the
        # hasattr checks and lazy-init branches in the loop are unnecessary.
        from .compress import CompactionState

        self._compaction_state: CompactionState = CompactionState()
        self._extractor = None
        self._overflow_retried = False
        self._steer_pending: str | None = None
        self.mode: str = "build"  # "build" | "plan"
        self._output_store = None  # lazy init
        self._active_subagents: list[SessionRunner] = []
        self._subagents_lock = asyncio.Lock()
        # Serializes run_turn calls on THIS runner (see run_turn docstring).
        # asyncio.Lock is held across the whole async generator — interrupt()
        # only sets a flag and never needs the lock, so no deadlock.
        self._turn_lock = asyncio.Lock()
        # Dedupe state for user-message persistence, keyed by session_id:
        # the actual store tail (role, content) per session as known to
        # this runner, kept current by _append. Keying matters because the
        # cron scheduler temporarily runs this runner under per-job session
        # ids — a single global tail would compare against the WRONG
        # session's messages after a cron tick.
        self._store_tail: dict[str, tuple[str, str]] = {}
        self._tail_checked: set[str] = set()

        # Per-session state (isolation between concurrent agents)
        from ..tools.builtins import browser as _br_module
        from ..tools.builtins import lsp as _lsp_module
        from ..tools.builtins import process as _proc_module
        from ..tools.builtins import todo_plan_exit as _tpe_module

        self._proc_registry = _proc_module.ProcRegistry()
        self._session_state = _tpe_module.SessionState()
        self._browser_state = _br_module.BrowserState()
        self._lsp_state = _lsp_module.LSPSessionState()
        # MCP connection managers spawned by mcp_connect — tracked so close()
        # can disconnect them. Without this, every mcp_connect() leaked a
        # subprocess (npx/uvx/...) for the lifetime of the Python process.
        self._mcp_managers: dict[str, object] = {}
        # Lock serializing mcp_connect's idempotency check+connect for THIS
        # runner. Owned by the runner (not ContextVar-lazy) because anyio
        # start_soon gives each _settle task a fresh context: a lazily
        # created ContextVar lock is per-TASK, so concurrent mcp_connect
        # calls in one turn each got their own lock and the double-spawn
        # race stayed open. Bound per-task in _settle like the managers
        # dict.
        self._mcp_connect_lock = asyncio.Lock()

        # Bind unconditionally — including None. Conditional binding leaks
        # the previous runner's values: two runners created in the same
        # context share it, so runner B (no store/loader) would otherwise
        # inherit runner A's ContextVar entries (session_search returning
        # another session's history).
        from ..tools.builtins import session_search as _ss
        from ..tools.builtins import skills_list as _sl_mod

        _ss._current_store.set(store)
        _sl_mod._set_loader(self.skill_loader)

        # Expose the per-session MCP manager dict to mcp_connect via ContextVar
        # so it populates *this* runner's dict (which close() will iterate).
        from ..tools.builtins import mcp_connect as _mcp_mod

        _mcp_mod._current_managers.set(self._mcp_managers)

        if self.memory is not None:
            from ..memory.extractor import MemoryExtractor

            self._extractor = MemoryExtractor(
                provider=self.memory,
                base_url=self.llm.config.base_url,
                api_key=self.llm.config.api_key,
                # Fire-and-forget extraction every turn shouldn't burn the
                # (usually expensive) main model when a cheaper auxiliary
                # model is configured.
                model=self.llm.config.auxiliary_model or self.llm.config.model,
            )

    async def close(self) -> None:
        """Clean up resources (memory extractor, browser page, LSP servers,
        background processes, MCP connections)."""
        # Close browser page if one was opened for this session
        if self._browser_state.page is not None:
            try:
                await self._browser_state.page.close()
            except Exception:
                pass
            self._browser_state.page = None

        # NOTE: the shared Chromium instance is deliberately NOT closed here.
        # It is a process-level singleton shared across sessions (browser.py);
        # runner.close() also runs for subagent child runners, so closing it
        # here would kill the parent's pages and any concurrent session's
        # browser. Process-level cleanup belongs to Agent.close().

        if self._extractor is not None:
            await self._extractor.close()

        # Shut down LSP servers
        for client in self._lsp_state.clients.values():
            try:
                await client.shutdown()
            except Exception:
                pass
        self._lsp_state.clients.clear()

        # Kill background processes started via the `process` tool.
        # Without this, `process start` leaves orphan subprocesses that
        # outlive the session and become zombies. Use the process GROUP
        # (process.py spawns with start_new_session=True) so grandchildren
        # — the actual workload, not just /bin/sh — are terminated too.
        # This mirrors process.py's own kill action.
        import os as _os
        import signal as _signal
        for sid, proc in list(self._proc_registry.procs.items()):
            if proc.returncode is None:
                try:
                    try:
                        _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        proc.kill()  # fallback: kill just the shell
                    # proc.wait() resolves only after the pipe transports
                    # drain. A process that filled its stdout/stderr pipes
                    # (yes, tail -f) has no reader here, so wait() never
                    # returns — Agent.close() and CLI exit would hang
                    # forever. Bound it; the process is dead either way.
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except TimeoutError:
                        pass  # killed; pipe drain may be stuck — proceed
                except Exception:
                    pass
        self._proc_registry.procs.clear()
        self._proc_registry.outputs.clear()

        # Disconnect MCP servers so their child subprocesses (npx, uvx, ...)
        # don't outlive the session.
        for mgr in list(self._mcp_managers.values()):
            try:
                await mgr.disconnect()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._mcp_managers.clear()

    async def resume(self, session_id: str, store: Store) -> tuple[Message, ...]:
        return tuple(await store.load_history(session_id))

    async def steer(self, text: str) -> None:
        """Inject a steer text into the running turn.

        The text will be appended to the most recent tool_result on the
        next iteration boundary. If the current iteration has no tool
        calls (pure text response), the steer waits until the next turn.
        Also propagates to active subagents (interrupt cascade).

        IMPORTANT: This method acquires _subagents_lock internally.
        Do NOT call steer() from code that already holds _subagents_lock
        — asyncio.Lock is not reentrant and this would deadlock.
        """
        self._steer_pending = text
        # Cascade to active subagents concurrently (snapshot under lock
        # to avoid holding it during potentially slow cascade).
        async with self._subagents_lock:
            children = list(self._active_subagents)
        if children:
            await asyncio.gather(*(child.steer(text) for child in children))

    # Tools blocked in plan mode (read-only mode).
    # bash is NOT in this set — plan mode allows read-only shell commands
    # (ls, cat, grep, find, git, etc.); destructive bash commands are
    # denied at execution time by _plan_bash_violation().
    # git (the tool) and mcp_connect are blocked too: the git tool
    # whitelists `commit`/`add` (repository mutation), and mcp_connect's
    # raw:<command> form executes arbitrary commands — both violate the
    # plan-mode read-only guarantee that _plan_bash_violation enforces
    # for bash.
    _PLAN_BLOCKED_TOOLS = frozenset({
        "write_file", "edit_file", "execute_code", "process",
        "browser_click", "browser_type", "browser_navigate",
        "browser_back", "browser_scroll", "browser_press",
        "git", "mcp_connect",
    })

    # First-token blocklist for plan-mode bash. Heuristic, not a sandbox:
    # it stops the common write/destructive verbs. A determined command
    # can still mutate state (e.g. `python -c ...`); full confinement
    # would require OS-level sandboxing, which is out of scope here.
    _PLAN_BASH_DESTRUCTIVE = frozenset({
        "rm", "rmdir", "mv", "cp", "chmod", "chown", "chgrp", "dd",
        "mkfs", "kill", "pkill", "killall", "shutdown", "reboot", "halt",
        "tee", "truncate", "install", "ln", "mkdir", "touch",
    })

    # git subcommands that mutate the repo or index.
    _PLAN_BASH_GIT_WRITE = frozenset({
        "add", "commit", "push", "reset", "checkout", "switch", "restore",
        "rebase", "merge", "cherry-pick", "revert", "apply", "clean",
        "rm", "mv", "tag", "stash", "branch", "am", "format-patch",
    })

    # Package-manager write subcommands (installing/uninstalling changes
    # the environment; `pip list` / `npm ls` stay allowed).
    _PLAN_BASH_PKG_WRITE = frozenset({
        "install", "uninstall", "remove", "add", "upgrade", "update",
    })

    _PLAN_SYSTEM_PROMPT = (
        "You are in **plan mode** — your job is to analyze and understand, "
        "NOT to make changes.\n\n"
        "Rules:\n"
        "1. Read files, search code, and explore the codebase to understand\n"
        "   the problem fully.\n"
        "2. You MAY use read-only bash commands (ls, cat, grep, find, git log,\n"
        "   git diff, git status, wc, head, tail, etc.). Never modify files,\n"
        "   run destructive commands (rm, mv, git commit, git push, chmod),\n"
        "   or install packages.\n"
        "3. Produce a clear analysis — findings, root causes, affected files,\n"
        "   and a recommended plan of action.\n"
        "4. Do NOT use write_file, edit_file, execute_code, process, git, or\n"
        "   mcp_connect. Use the plan tool to document your multi-step action plan.\n"
        "5. When you're done analyzing, end your final message with the exact\n"
        "   text '/build' on its own line so the user can switch to build mode\n"
        "   to execute your plan."
    )

    @classmethod
    def _plan_bash_violation(cls, command: str) -> str | None:
        """Return a denial reason if a plan-mode bash command looks
        write/destructive, else None (allowed).

        Heuristic: each pipeline/list segment is shlex-split; the first
        token is checked against the destructive verb set, git/package
        write subcommands, and `sed -i`. Output redirection (>, >>)
        outside quotes is also denied. Quoted '>' (e.g. echo 'a>b') is
        treated as data. Known limitations: `echo a>b` unquoted and
        arbitrary interpreters (`python -c ...`) are not caught.
        """
        import re
        import shlex

        for segment in re.split(r";|\|\||&&|\|", command):
            segment = segment.strip()
            if not segment:
                continue
            try:
                tokens = shlex.split(segment, posix=True)
            except ValueError:
                tokens = segment.split()
            if not tokens:
                continue
            if any(t in (">", ">>") or t.startswith(">") for t in tokens):
                return "output redirection is not allowed in plan mode"
            verb = tokens[0].rsplit("/", 1)[-1]
            if verb in cls._PLAN_BASH_DESTRUCTIVE:
                return f"'{verb}' modifies the system — not allowed in plan mode"
            if verb == "git" and len(tokens) > 1 and tokens[1] in cls._PLAN_BASH_GIT_WRITE:
                return f"'git {tokens[1]}' modifies the repository — not allowed in plan mode"
            if verb == "sed" and any(t.startswith("-i") for t in tokens[1:]):
                return "'sed -i' edits files in place — not allowed in plan mode"
            if verb in ("pip", "pip3", "uv", "npm", "pnpm", "yarn", "brew", "apt", "apt-get") \
                    and len(tokens) > 1 and tokens[1] in cls._PLAN_BASH_PKG_WRITE:
                return f"'{verb} {tokens[1]}' modifies the environment — not allowed in plan mode"
        return None

    async def _process_tool_output_async(self, tool_call_id: str, result, sid: str) -> Any:
        """Apply ToolOutputStore size management to tool results (non-blocking)."""
        from ..tools.output_store import ToolOutputStore

        if self._output_store is None:
            self._output_store = ToolOutputStore()
        processed = await self._output_store.process_async(
            tool_call_id, result.content, result.metadata.get("tool_name", "unknown") if result.metadata else "unknown",
            session_id=sid,
        )
        if processed.saved_to_disk:
            return type(result)(content=processed.content, is_error=result.is_error, metadata=result.metadata)
        return result

    def _get_available_tools(self) -> set[str]:
        """Return set of tool names available in current mode."""
        all_names = set(self.registry.names)
        if self.mode == "plan":
            return all_names - self._PLAN_BLOCKED_TOOLS
        return all_names

    def interrupt(self) -> None:
        """Request interrupt of the current turn.

        Sets a flag that is checked between LLM stream events and tool
        executions. The runner will yield TurnFailed and return.
        """
        self._interrupt_requested = True

    async def run_turn(
        self,
        messages: list[Message],
    ) -> AsyncIterator[Event]:
        """Run a conversation turn with streaming tool output.

        Serialized per runner: two concurrent run_turn calls previously
        interleaved their store writes (user-A,user-B,assistant-A,assistant-B),
        producing a persisted conversation that never actually happened on
        resume. The turn lock makes them strictly sequential.
        Cross-INSTANCE sharing of one session_id is NOT guarded — embedders
        must not point two runners at the same session concurrently.

        The session id is captured ONCE under the turn lock: the cron
        scheduler swaps runner.session_id around its own arun, and an
        in-flight turn must keep writing to the session it started in.
        """
        async with self._turn_lock:
            sid = self.session_id
            async for event in self._run_turn_inner(messages, sid):
                yield event

    async def _persist_user_tail(self, last: Message, sid: str) -> None:
        """Append the trailing user message to the store, deduplicated.

        Two paths used to write the SAME message twice: (a) resume() loads
        a history whose last message is an unanswered user message (crash
        before the assistant reply) and the caller passes it back in;
        (b) a turn ends in TurnFailed (budget/interrupt) and the caller
        retries with the same messages list. Both produced duplicate user
        messages in the store — and on resume, history the model never
        actually saw. Skip the append when the store tail already IS this
        message. Identical consecutive user texts are not falsely skipped:
        a completed turn leaves an assistant (or tool) tail, so the guard
        only fires when nothing was persisted after that user message.
        """
        if sid not in self._tail_checked:
            self._tail_checked.add(sid)
            try:
                history = await self.store.load_history(sid)
            except Exception:
                history = []
            if history:
                self._store_tail[sid] = (history[-1].role, history[-1].content)
        if self._store_tail.get(sid) == ("user", last.content):
            return
        await self._append(sid, last)

    async def _append(self, session_id: str, msg: Message) -> None:
        """Store append that keeps the known store tail current — the
        user-message dedupe in _persist_user_tail relies on it."""
        await self.store.append(session_id, msg)
        self._store_tail[session_id] = (msg.role, msg.content)

    async def _run_turn_inner(
        self,
        messages: list[Message],
        sid: str,
    ) -> AsyncIterator[Event]:
        """Turn implementation — must only be entered under _turn_lock.

        ``sid`` is the session id captured under the turn lock; all store
        writes and events in this turn use it, immune to mid-turn swaps of
        ``self.session_id`` (the cron scheduler does that around its arun).
        """

        if self.store is not None and messages:
            last = messages[-1]
            if last.role == "user":
                await self._persist_user_tail(last, sid)

        self._overflow_retried = False
        self._stream_retried = False  # one-shot stream-error retry per turn
        self._stream_retry_free = False  # next loop pass is the retry — don't charge an iteration
        self._interrupt_requested = False
        # NOTE: _steer_pending is intentionally NOT cleared here. A steer
        # arriving during a pure-text turn is documented to "wait until the
        # next turn" (steer() docstring + test_steer_pure_text_response_waits).
        # Clearing it at entry would discard a legitimately-pending steer.
        # A previously-applied entry-clear (commit a438b7f) was reverted for
        # this reason — it broke the documented persistence contract.
        # Reset anti-jitter counter once per user turn (not per loop
        # iteration). Previously this lived inside the while loop, so the
        # counter was reset on every tool-call iteration — it could never
        # reach the skip threshold, and ineffective compression retried
        # every iteration, burning LLM tokens / budget.
        self._compaction_state.reset_for_new_turn()

        while not self.budget.exhausted:
            if self._interrupt_requested:
                yield TurnFailed("interrupted by user")
                return

            # A stream-error retry re-enters the loop for the SAME logical
            # LLM call. Without this skip the retry pass charges a second
            # iteration, so every one-shot retry silently halved the turn
            # budget available after a network blip (default 25 → ~12 real
            # calls). The retry is free: it produces no new output.
            if not self._stream_retry_free:
                try:
                    await self.budget.consume(iterations=1)
                except BudgetExceeded as e:
                    yield TurnFailed(str(e))
                    return
            self._stream_retry_free = False

            # System prompt is frozen (ADR-0005) — skills/memory/context
            # sources are injected into the user message, not system prompt.

            _threshold = self.compression_threshold
            if _threshold <= 0:
                from ..llm.client import get_context_window

                _window = get_context_window(self.llm.config.model)
                _threshold = int(_window * 0.6)

            from .compress import compact_conversation, count_tokens

            # Anti-jitter: skip if 2 consecutive ineffective compressions.
            # The gate is purely token-based — a message-count gate
            # (len > 10) let a 3-message conversation with huge content
            # blow past the threshold without ever compacting.
            if self._compaction_state.should_skip_compression():
                pass  # skip auto-compression
            else:
                before_tokens = count_tokens(tuple(messages))
                if before_tokens > _threshold:
                    try:
                        # Use auxiliary model for compression if configured
                        compress_llm = self.llm
                        if self.llm.config.auxiliary_model:
                            compress_llm = self.llm.for_model(self.llm.config.auxiliary_model)
                        messages_list = await compact_conversation(
                            tuple(messages),
                            compress_llm,
                            context_window=_threshold + 8000,
                            state=self._compaction_state,
                            budget=self.budget,
                        )
                        messages[:] = list(messages_list)
                        # Track compression effectiveness (anti-jitter)
                        after_tokens = count_tokens(tuple(messages))
                        if before_tokens > 0 and (before_tokens - after_tokens) / before_tokens < 0.1:
                            self._compaction_state.record_ineffective()
                        else:
                            self._compaction_state.record_success()
                    except BudgetExceeded as e:
                        yield TurnFailed(f"budget exhausted during compaction: {e}")
                        return

            system = self.system_prompt

            # Compose user prompt + model-specific template (both preserved)
            from ..llm.templates import build_system_prompt

            system = build_system_prompt(self.llm.config.model, system)

            # Override with plan-mode prompt when appropriate.
            # Skill catalog is appended AFTER this so plan mode still sees it.
            if self.mode == "plan":
                system = self._PLAN_SYSTEM_PROMPT

            # Build skill catalog for system prompt (stable, cached).
            # This lets the LLM know what skills are available so it can
            # request them via skills_list or skill_manage.
            skill_catalog = ""
            if self.skill_loader is not None:
                if not self._cached_skill_catalog:
                    try:
                        all_skills = await self.skill_loader.load()
                        if all_skills:
                            catalog_lines = ["## Available Skills\n"]
                            for s in all_skills:
                                desc = s.description[:80] if s.description else "(no description)"
                                catalog_lines.append(
                                    f"- **{s.name}** ({s.namespace}): {desc}"
                                )
                            self._cached_skill_catalog = "\n".join(catalog_lines)
                    except Exception:
                        pass
                skill_catalog = self._cached_skill_catalog

            # Append skill catalog to system prompt (frozen layer — cached)
            if skill_catalog:
                system = system + "\n\n" + skill_catalog

            # Build context injection block for user message
            context_parts: list[str] = []

            # Skill matching: keywords + CJK-aware fuzzy.
            # Once matched, skills stay loaded for the session (persistent).
            # disabled_skills (CLI /skill unload) filters both matching and
            # body injection — without it the unload command was cosmetic.
            if self.skill_loader is not None and messages:
                last_user = next((m for m in reversed(messages) if m.role == "user"), None)
                if last_user:
                    try:
                        matched = await self.skill_loader.match(last_user.content)
                        for m in matched:
                            key = f"{m.skill.namespace}:{m.skill.name}"
                            if m.skill.name in self.disabled_skills or key in self.disabled_skills:
                                continue
                            # LRU via OrderedDict: move_to_end on access,
                            # popitem(last=False) to evict oldest.
                            if key in self._loaded_skills:
                                self._loaded_skills.move_to_end(key)
                            else:
                                self._loaded_skills[key] = None
                                while len(self._loaded_skills) > self._max_loaded_skills:
                                    self._loaded_skills.popitem(last=False)

                        # Inject all loaded skill bodies as context
                        if self._loaded_skills:
                            all_skills = {s.name: s for s in (await self.skill_loader.load())}
                            loaded_bodies = []
                            for key in self._loaded_skills:
                                ns, name = key.split(":", 1)
                                if name in self.disabled_skills or key in self.disabled_skills:
                                    continue
                                s = all_skills.get(name)
                                if s is not None:
                                    loaded_bodies.append(s.body)
                            if loaded_bodies:
                                context_parts.append(
                                    "## Loaded Skills\n\n" + "\n---\n".join(loaded_bodies)
                                )
                    except Exception:
                        pass

            for src in self.context_sources:
                try:
                    contribution = await src.contribute(None)
                except Exception:
                    # A failing ContextSource (network lookup, bug in a
                    # plugin) must not crash the whole turn — same
                    # fault-tolerance contract as the skill loader above.
                    logger.warning("context source %r failed", src, exc_info=True)
                    continue
                if contribution:
                    context_parts.append(contribution)

            # Scan context for injection patterns before injecting
            if context_parts:
                from ..security.patterns import scan_for_injection

                scanned_parts = []
                for part in context_parts:
                    result = scan_for_injection(part)
                    if result.blocked:
                        scanned_parts.append(result.sanitized)
                    else:
                        scanned_parts.append(part)
                context_parts = scanned_parts

            # Inject context into the last user message (frozen system prompt)
            send_messages = messages
            if context_parts:
                context_block = "<context>\n" + "\n\n".join(context_parts) + "\n</context>"
                # Find last user message and append context
                send_messages = list(messages)
                for i in range(len(send_messages) - 1, -1, -1):
                    if send_messages[i].role == "user":
                        send_messages[i] = Message.user(
                            send_messages[i].content + "\n\n" + context_block
                        )
                        break

            # Run pre_llm_hooks. Cache the hooked system to detect when
            # hooks produce deterministic output (same input → same output).
            # Non-deterministic hooks (e.g. timestamp injection) will
            # naturally invalidate the cache every turn — that's correct.
            hooked_system = system
            for hook in self.pre_llm_hooks:
                try:
                    hooked_system = await hook(hooked_system)
                except Exception:
                    # Keep the last good system prompt; a broken hook must
                    # not abort the turn.
                    logger.warning("pre_llm_hook %r failed", hook, exc_info=True)

            # Rebuild the cached tools snapshot when the system prompt or
            # mode changes — OR when the registry itself changed (mcp_connect
            # registers tools mid-session; without the version check they
            # never reached the LLM's tools list).
            if (
                self._cached_system is None
                or hooked_system != self._cached_system
                or self.mode != self._cached_mode
                or self.registry.version != self._cached_tools_version
            ):
                self._cached_system = hooked_system
                self._cached_mode = self.mode
                self._cached_tools_version = self.registry.version
                all_tools = self.registry.to_openai_tools() or None
                # Filter tools by mode (plan mode blocks write tools)
                if all_tools and self.mode == "plan":
                    available = self._get_available_tools()
                    all_tools = [t for t in all_tools if t.get("function", {}).get("name", "") in available] or None
                self._cached_tools = all_tools
            system = self._cached_system
            oai_tools = self._cached_tools

            content_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            usage: Usage | None = None
            _overflow_retrying = False
            # Tracks whether any user-visible output (content/thinking/
            # tool-call deltas) has been yielded from THIS stream attempt —
            # a retry after partial output would duplicate it on screen.
            _stream_got_output = False
            try:
                async for event in self.llm.stream(
                    system=system,
                    messages=tuple(send_messages),
                    tools=oai_tools,
                ):
                    if self._interrupt_requested:
                        yield TurnFailed("interrupted by user")
                        return

                    if isinstance(event, TextDelta):
                        # Only kind="content" counts as the assistant's reply.
                        # Thinking deltas are yielded for display but must not
                        # enter content_parts — otherwise reasoning text gets
                        # persisted into the assistant message, and a
                        # thinking-only + stop_reason="length" stream would be
                        # misclassified as truncation instead of overflow.
                        if event.kind == "content":
                            content_parts.append(event.text)
                        _stream_got_output = True
                        yield event
                    elif isinstance(event, ToolCallDelta):
                        tc = ToolCall(id=event.id, name=event.name, arguments=event.arguments)
                        tool_calls.append(tc)
                        # Forward to consumers so the CLI's 🔧 tool-call panel
                        # can render the tool name + args BEFORE execution (the
                        # ✓ result panel follows once the tool runs). Previously
                        # this was consumed but not re-yielded, making the panel
                        # handler dead code — same class as the Usage-swallow bug
                        # fixed in 6c24b81.
                        _stream_got_output = True
                        yield event
                    elif isinstance(event, Usage):
                        usage = event
                    elif isinstance(event, StreamDone):
                        usage = event.usage
                        if event.stop_reason == "length":
                            # Overflow: no user-visible content → compact + retry.
                            # Tool calls from a truncated stream have incomplete
                            # argument JSON by definition — executing them then
                            # retrying wastes LLM calls, so drop them and treat
                            # this as an overflow.
                            if not content_parts:
                                if not self._overflow_retried:
                                    self._overflow_retried = True
                                    if usage:
                                        try:
                                            await self.budget.consume_usage(usage)
                                        except BudgetExceeded as e:
                                            yield TurnFailed(str(e))
                                            return
                                    # Force compaction to reduce context.
                                    # Use the auxiliary model like the
                                    # auto-compression path above — the main
                                    # model just failed on this context size,
                                    # and the aux model is the cheaper one
                                    # for a rescue operation.
                                    from .compress import compact_conversation

                                    try:
                                        compress_llm = self.llm
                                        if self.llm.config.auxiliary_model:
                                            compress_llm = self.llm.for_model(
                                                self.llm.config.auxiliary_model
                                            )
                                        messages_list = await compact_conversation(
                                            tuple(messages),
                                            compress_llm,
                                            context_window=_threshold + 8000,
                                            state=self._compaction_state,
                                            force=True,
                                            budget=self.budget,
                                        )
                                        messages[:] = list(messages_list)
                                    except BudgetExceeded as e:
                                        yield TurnFailed(f"budget exhausted during overflow recovery: {e}")
                                        return
                                    except Exception:
                                        yield TurnFailed("overflow recovery: compaction failed")
                                        return
                                    _overflow_retrying = True
                                    break  # break out of async for → while loop retries
                                else:
                                    # Already retried — fail
                                    yield TurnFailed("LLM overflow recovery failed after retry")
                                    return

                            # Content was streamed (truncation) — fail.
                            # Do NOT persist partial tool_calls: they would be
                            # orphaned (no matching tool results), and the OpenAI
                            # API rejects the next turn with "messages must contain
                            # tool results for all tool calls". Keep only the text.
                            if content_parts:
                                assistant_msg = Message.assistant(
                                    text="".join(content_parts),
                                    usage=usage,
                                )
                                messages.append(assistant_msg)
                                if self.store is not None:
                                    await self._append(sid, assistant_msg)
                                if usage:
                                    try:
                                        await self.budget.consume_usage(usage)
                                    except BudgetExceeded as e:
                                        yield TurnFailed(str(e))
                                        return
                                yield TurnFailed("LLM response truncated (max tokens)")
                                return
                            # Content was streamed but handled above. If neither
                            # content nor tool_calls exist this is also an
                            # overflow — covered by the same branch.

            except Exception as e:
                # LLM stream failed (network drop, gateway 5xx, auth
                # rejection after credential rotation, ...). Without this
                # guard the raw exception escapes run_turn and crashes
                # Agent.arun callers. Retry ONCE only if no user-visible
                # output was produced yet (retrying after partial content
                # would duplicate text on screen). CancelledError is not
                # caught here — it must keep propagating for interrupt.
                if not self._stream_retried and not _stream_got_output:
                    self._stream_retried = True
                    self._stream_retry_free = True  # don't charge the retry pass
                    continue  # retry the turn (re-enters the outer loop)
                yield TurnFailed(f"LLM error: {e!r}")
                return

            if _overflow_retrying:
                continue  # retry the turn after overflow recovery

            assistant_msg = Message.assistant(
                text="".join(content_parts),
                tool_calls=tuple(tool_calls),
                usage=usage,
            )

            messages.append(assistant_msg)

            if self.store is not None:
                await self._append(sid, assistant_msg)

            if usage:
                yield usage
                try:
                    await self.budget.consume_usage(usage)
                except BudgetExceeded as e:
                    # The assistant message with its tool_calls was ALREADY
                    # persisted at this point. If we return now, the store
                    # holds orphaned tool_calls with no matching tool
                    # results, and the OpenAI API rejects the resumed
                    # session ("messages must contain tool results for all
                    # tool calls"). The interrupt path (BaseException
                    # handler below) persists error results for exactly
                    # this reason — the budget path must not skip that
                    # contract. Persist an error result for every tool
                    # call and strip them from the in-memory assistant
                    # message so the turn state stays consistent too.
                    for tc in tool_calls:
                        msg = Message.tool_result(
                            ToolResult.error("budget exhausted: tool not executed"),
                            tool_call_id=tc.id,
                        )
                        messages.append(msg)
                        if self.store is not None:
                            await self._append(sid, msg)
                    # In-memory assistant message: drop the never-executed
                    # tool calls so it matches the store (text preserved).
                    if assistant_msg in messages:
                        idx = messages.index(assistant_msg)
                        messages[idx] = Message.assistant(
                            text=assistant_msg.content,
                            usage=assistant_msg.usage,
                        )
                    yield TurnFailed(str(e))
                    return

            if not tool_calls:
                if self.event_bus:
                    await self.event_bus.emit(
                        "turn_complete", sid, assistant_msg.content
                    )
                if self._extractor is not None:
                    try:
                        history = tuple(
                            {"role": m.role, "content": m.content} for m in messages[-10:]
                        )
                        await self._extractor.extract_async(history)
                    except Exception:
                        pass
                yield TurnComplete(assistant_msg.content)
                return

            # --- 4. Execute tool calls with streaming ---
            try:
                results, progress_events = await self._run_tool_calls(tool_calls)
            except BaseException:
                # Hard cancel (task.cancel / Ctrl-C) mid-execution: the
                # assistant message with these tool_calls is already
                # persisted, so persist an error result for every tool
                # call that never settled. Without this the store holds
                # orphaned tool_calls and the OpenAI API rejects the
                # resumed session ("messages must contain tool results
                # for all tool calls"). Tool results are only persisted
                # after _run_tool_calls returns, so none are missing.
                for tc in tool_calls:
                    msg = Message.tool_result(
                        ToolResult.error("interrupted: tool execution cancelled"),
                        tool_call_id=tc.id,
                    )
                    messages.append(msg)
                    if self.store is not None:
                        await self._append(sid, msg)
                raise

            # Yield progress events before results (real-time UX)
            for pe in progress_events:
                yield pe

            for tc, result in zip(tool_calls, results):
                # Apply output size management if result is large. Vision
                # tools are exempt: their base64 data URLs must reach the
                # model intact (see _OUTPUT_STORE_EXEMPT).
                if tc.name in _OUTPUT_STORE_EXEMPT:
                    processed = result
                else:
                    processed = await self._process_tool_output_async(tc.id, result, sid)
                msg = Message.tool_result(processed, tool_call_id=tc.id)
                messages.append(msg)
                if self.store is not None:
                    await self._append(sid, msg)
                yield ToolResultDelta(
                    id=tc.id,
                    name=tc.name,
                    content=result.content[:200],
                    is_error=result.is_error,
                )

            # Inject steer text into the last tool_result if pending.
            # NOTE: the modified message is in-memory only — we do NOT
            # store.append() it, because that would persist a second tool
            # message with the same tool_call_id and corrupt the session
            # (OpenAI API rejects duplicate tool_call_id on resume). The
            # original tool result stays in the store unchanged; the steer
            # is a live intervention (like interrupt), not persisted state.
            if self._steer_pending is not None and messages:
                steer_text = self._steer_pending
                self._steer_pending = None
                yield SteerEvent(text=steer_text)
                # Find last tool message and append steer text (in-memory only)
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i].role == "tool":
                        messages[i] = Message(
                            role="tool",
                            content=messages[i].content + f"\n\n[steer] {steer_text}",
                            tool_call_id=messages[i].tool_call_id,
                        )
                        break

            # The exit tool's contract is "the session should end" — its
            # [SESSION_EXIT] marker must actually terminate the turn here.
            # Previously NOTHING consumed the marker: the loop continued,
            # the LLM got another iteration, and the exit call was a no-op.
            if any(r.content.strip() == "[SESSION_EXIT]" for r in results):
                yield TurnComplete("(session ended by exit tool)")
                return

        yield TurnFailed(f"budget exhausted after {self.budget.max_iterations} iterations")

    async def _run_tool_calls(
        self, calls: list[ToolCall]
    ) -> tuple[list[ToolResult], list[ToolProgressDelta]]:
        """Execute tool calls concurrently, collecting progress events."""
        results: list[ToolResult | None] = [None] * len(calls)
        progress_events: list[ToolProgressDelta] = []

        async def _settle(idx: int, call: ToolCall) -> None:
            try:
                from ..tools.builtins import browser as _br_module
                from ..tools.builtins import lsp as _lsp_module
                from ..tools.builtins import mcp_connect as _mcp_module
                from ..tools.builtins import process as _proc_module
                from ..tools.builtins import skills_list as _sl_mod
                from ..tools.builtins import task as _task_module
                from ..tools.builtins import todo_plan_exit as _tpe_module

                # Re-bind ALL per-session ContextVars per task. anyio
                # start_soon copies the current context, but if two
                # SessionRunners were created in the same context (the common
                # pattern), the second __init__ overwrites the first's values
                # in the shared context. _settle runs inside each task's own
                # copy, so setting here ensures isolation. Bind store and
                # loader unconditionally (including None) — conditional
                # binding lets a runner without them inherit a sibling
                # runner's values from the shared creation context.
                _proc_module._current_registry.set(self._proc_registry)
                _tpe_module._current_state.set(self._session_state)
                _br_module._current_state.set(self._browser_state)
                _lsp_module._current_state.set(self._lsp_state)
                _task_module._current_runner.set(self)
                _mcp_module._current_managers.set(self._mcp_managers)
                _mcp_module._current_lock.set(self._mcp_connect_lock)
                from ..tools.builtins import session_search as _ss
                _ss._current_store.set(self.store)
                _sl_mod._set_loader(self.skill_loader)

                modified = call
                for hook in self.tool_hooks:
                    modified = await hook.before(modified, None)
                    if modified is None:
                        results[idx] = ToolResult.denied("blocked by tool hook")
                        return
                    call = modified

                # Plan-mode hard guard at the execution layer. The tool
                # list sent to the LLM is already filtered, but a model
                # can still emit a write tool call (fine-tuned habits,
                # prompt injection) — enforce the read-only guarantee here.
                if self.mode == "plan":
                    if call.name in self._PLAN_BLOCKED_TOOLS:
                        results[idx] = ToolResult.denied(
                            f"plan mode: '{call.name}' is a write tool and is blocked"
                        )
                        return
                    if call.name == "bash":
                        reason = self._plan_bash_violation(
                            str(call.arguments.get("command", ""))
                        )
                        if reason is not None:
                            results[idx] = ToolResult.denied(f"plan mode: {reason}")
                            return

                # Permission engine check — enforce rule-based access control
                # after plan-mode guard, before execution.
                if self.permission_engine is not None:
                    decision = await self.permission_engine.evaluate(call)
                    if decision.is_deny:
                        results[idx] = ToolResult.denied(
                            f"permission denied: {decision.reason or 'no rule matched'}"
                        )
                        return

                # Streaming execution — ToolRegistry.execute_stream always
                # exists (core/tool.py) and has its own non-streaming fallback
                # internally, so no try/except fallback is needed here.
                async for event in self.registry.execute_stream(call):
                    if isinstance(event, ToolProgressDelta):
                        progress_events.append(event)
                    elif isinstance(event, ToolResult):
                        result = event
                        break
                else:
                    result = ToolResult.ok("(no output)")

                for hook in self.tool_hooks:
                    result = await hook.after(call, result, None)

                results[idx] = result
            except Exception as e:
                results[idx] = ToolResult.error(f"{call.name} failed: {e!r}")

        async with anyio.create_task_group() as tg:
            async def _interrupt_watcher() -> None:
                # Preemptive interrupt: poll the flag while any tool is
                # still running and cancel the whole task group when it
                # flips. Without this, interrupt() only takes effect at
                # the next LLM stream boundary — a `sleep 60` tool would
                # ignore it. Exits as soon as every call has settled so
                # the task group can complete normally.
                while any(r is None for r in results):
                    if self._interrupt_requested:
                        tg.cancel_scope.cancel()
                        return
                    await anyio.sleep(0.05)

            tg.start_soon(_interrupt_watcher)
            for idx, call in enumerate(calls):
                tg.start_soon(_settle, idx, call)

        return (
            [r if r is not None else ToolResult.error("not executed (cancelled)") for r in results],
            progress_events,
        )
