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
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

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

        self._cached_system: str | None = None
        self._cached_tools: list[dict] | None = None
        self._cached_skill_catalog: str = ""  # stable part of system prompt
        # OrderedDict preserves insertion order + supports move_to_end/popitem
        # for LRU eviction. Replaces the prior hand-rolled set+list pair.
        self._loaded_skills: OrderedDict[str, None] = OrderedDict()
        self._max_loaded_skills: int = 10  # cap to prevent unbounded growth
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

        if store is not None:
            from ..tools.builtins import session_search as _ss

            _ss._current_store.set(store)

        if self.skill_loader is not None:
            from ..tools.builtins import skills_list as _sl_mod

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
                model=self.llm.config.model,
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
                    await proc.wait()
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
    # (ls, cat, grep, find, git, etc.). Destructive commands are blocked
    # by the plan-mode system prompt instructing the LLM not to execute
    # modifications, not by tool filtering.
    _PLAN_BLOCKED_TOOLS = frozenset({
        "write_file", "edit_file", "execute_code", "process",
        "browser_click", "browser_type", "browser_navigate",
        "browser_back", "browser_scroll", "browser_press",
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
        "4. Do NOT use write_file, edit_file, execute_code, or process. Use the\n"
        "   plan tool to document your multi-step action plan.\n"
        "5. When you're done analyzing, end your final message with the exact\n"
        "   text '/build' on its own line so the user can switch to build mode\n"
        "   to execute your plan."
    )

    def _process_tool_output(self, tool_call_id: str, result) -> Any:
        """Apply ToolOutputStore size management to tool results."""
        from ..tools.output_store import ToolOutputStore

        if self._output_store is None:
            self._output_store = ToolOutputStore()
        processed = self._output_store.process(
            tool_call_id, result.content, result.metadata.get("tool_name", "unknown") if result.metadata else "unknown",
            session_id=self.session_id,
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
        """Run a conversation turn with streaming tool output."""

        if self.store is not None and messages:
            last = messages[-1]
            if last.role == "user":
                await self.store.append(self.session_id, last)

        self._overflow_retried = False
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

            try:
                await self.budget.consume(iterations=1)
            except BudgetExceeded as e:
                yield TurnFailed(f"budget exhausted: {e}")
                return

            # System prompt is frozen (ADR-0005) — skills/memory/context
            # sources are injected into the user message, not system prompt.

            _threshold = self.compression_threshold
            if _threshold <= 0:
                from ..llm.client import get_context_window

                _window = get_context_window(self.llm.config.model)
                _threshold = int(_window * 0.6)

            if len(messages) > 10:
                from .compress import compact_conversation, count_tokens

                # Anti-jitter: skip if 2 consecutive ineffective compressions
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
            if self.skill_loader is not None and messages:
                last_user = next((m for m in reversed(messages) if m.role == "user"), None)
                if last_user:
                    try:
                        matched = await self.skill_loader.match(last_user.content)
                        for m in matched:
                            key = f"{m.skill.namespace}:{m.skill.name}"
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
                contribution = await src.contribute(None)
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
                hooked_system = await hook(hooked_system)

            if self._cached_system is None or hooked_system != self._cached_system or self.mode != self._cached_mode:
                self._cached_system = hooked_system
                self._cached_mode = self.mode
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

            async for event in self.llm.stream(
                system=system,
                messages=tuple(send_messages),
                tools=oai_tools,
            ):
                if self._interrupt_requested:
                    yield TurnFailed("interrupted by user")
                    return

                if isinstance(event, TextDelta):
                    content_parts.append(event.text)
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
                    yield event
                elif isinstance(event, Usage):
                    usage = event
                elif isinstance(event, StreamDone):
                    usage = event.usage
                    if event.stop_reason == "length":
                        # Overflow: no content AND no tool calls → compact + retry
                        if not content_parts and not tool_calls:
                            if not self._overflow_retried:
                                self._overflow_retried = True
                                if usage:
                                    try:
                                        await self.budget.consume_usage(usage)
                                    except BudgetExceeded as e:
                                        yield TurnFailed(f"budget exhausted: {e}")
                                        return
                                # Force compaction to reduce context
                                from .compress import compact_conversation

                                try:
                                    messages_list = await compact_conversation(
                                        tuple(messages),
                                        self.llm,
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
                                await self.store.append(self.session_id, assistant_msg)
                            if usage:
                                try:
                                    await self.budget.consume_usage(usage)
                                except BudgetExceeded as e:
                                    yield TurnFailed(f"budget exhausted: {e}")
                                    return
                            yield TurnFailed("LLM response truncated (max tokens)")
                            return
                        # Tool calls present, no content — proceed normally (not an overflow)

            if _overflow_retrying:
                continue  # retry the turn after overflow recovery

            assistant_msg = Message.assistant(
                text="".join(content_parts),
                tool_calls=tuple(tool_calls),
                usage=usage,
            )

            messages.append(assistant_msg)

            if self.store is not None:
                await self.store.append(self.session_id, assistant_msg)

            if usage:
                yield usage
                try:
                    await self.budget.consume_usage(usage)
                except BudgetExceeded as e:
                    yield TurnFailed(f"budget exhausted: {e}")
                    return

            if not tool_calls:
                if self.event_bus:
                    await self.event_bus.emit(
                        "turn_complete", self.session_id, assistant_msg.content
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
            results, progress_events = await self._run_tool_calls(tool_calls)

            # Yield progress events before results (real-time UX)
            for pe in progress_events:
                yield pe

            for tc, result in zip(tool_calls, results):
                # Apply output size management if result is large
                processed = self._process_tool_output(tc.id, result)
                msg = Message.tool_result(processed, tool_call_id=tc.id)
                messages.append(msg)
                if self.store is not None:
                    await self.store.append(self.session_id, msg)
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
                # copy, so setting here ensures isolation. Previously
                # _current_store and _current_loader were missed (set only
                # in __init__), causing session_search and skills_list to
                # cross-contaminate between concurrent sessions.
                _proc_module._current_registry.set(self._proc_registry)
                _tpe_module._current_state.set(self._session_state)
                _br_module._current_state.set(self._browser_state)
                _lsp_module._current_state.set(self._lsp_state)
                _task_module._current_runner.set(self)
                _mcp_module._current_managers.set(self._mcp_managers)
                if self.store is not None:
                    from ..tools.builtins import session_search as _ss
                    _ss._current_store.set(self.store)
                if self.skill_loader is not None:
                    _sl_mod._set_loader(self.skill_loader)

                modified = call
                for hook in self.tool_hooks:
                    modified = await hook.before(modified, None)
                    if modified is None:
                        results[idx] = ToolResult.denied("blocked by tool hook")
                        return
                    call = modified

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
            for idx, call in enumerate(calls):
                tg.start_soon(_settle, idx, call)

        return (
            [r if r is not None else ToolResult.error("not executed (cancelled)") for r in results],
            progress_events,
        )
