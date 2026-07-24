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
from typing import AsyncIterator

import anyio

from ..core.types import (
    Message, ToolCall, ToolResult,
    TextDelta, ToolCallDelta, ToolProgressDelta, ToolResultDelta,
    TurnComplete, TurnFailed, Event, Usage,
)
from ..core.tool import ToolRegistry
from ..core.store import Store
from ..llm.client import LLMClient, StreamDone
from ..core.event import EventBus
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
        store: "Store | None" = None,
        session_id: str = "default",
        event_bus: "EventBus | None" = None,
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
        self._extractor = None

        if store is not None:
            from ..tools.builtins import session_search as _ss
            _ss._current_store.set(store)

        if self.memory is not None:
            from ..memory.extractor import MemoryExtractor
            self._extractor = MemoryExtractor(provider=self.memory)

    async def resume(
        self, session_id: str, store: Store
    ) -> tuple[Message, ...]:
        return tuple(await store.load_history(session_id))

    async def run_turn(
        self,
        messages: list[Message],
    ) -> AsyncIterator[Event]:
        """Run a conversation turn with streaming tool output."""

        if self.store is not None and messages:
            last = messages[-1]
            if last.role == "user":
                await self.store.append(self.session_id, last)

        while not self.budget.exhausted:
            try:
                self.budget.consume(iterations=1)
            except BudgetExceeded as e:
                yield TurnFailed(f"budget exhausted: {e}")
                return

            system = self.system_prompt

            _threshold = self.compression_threshold
            if _threshold <= 0:
                from ..llm.client import get_context_window
                _window = get_context_window(self.llm.config.model)
                _threshold = int(_window * 0.6)

            if len(messages) > 10:
                from .compress import count_tokens, compact_conversation, CompactionState
                if count_tokens(tuple(messages)) > _threshold:
                    if not hasattr(self, '_compaction_state'):
                        self._compaction_state = CompactionState()
                    messages_list = await compact_conversation(
                        tuple(messages), self.llm,
                        context_window=_threshold + 8000,
                        state=self._compaction_state,
                    )
                    messages[:] = list(messages_list)

            if self.skill_loader is not None and messages:
                last_user = next((m for m in reversed(messages) if m.role == "user"), None)
                if last_user:
                    try:
                        matched = await self.skill_loader.match(last_user.content)
                        if matched:
                            skill_texts = [m.skill.body for m in matched[:3]]
                            system += "\n\n## Relevant Skills\n\n" + "\n---\n".join(skill_texts)
                    except Exception:
                        pass

            for src in self.context_sources:
                system += await src.contribute(None)

            for hook in self.pre_llm_hooks:
                system = await hook(system)

            if self._cached_system is None or system != self._cached_system:
                self._cached_system = system
                self._cached_tools = self.registry.to_openai_tools() or None
            oai_tools = self._cached_tools

            content_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            usage: Usage | None = None

            async for event in self.llm.stream(
                system=system,
                messages=tuple(messages),
                tools=oai_tools,
            ):
                if isinstance(event, TextDelta):
                    content_parts.append(event.text)
                    yield event
                elif isinstance(event, ToolCallDelta):
                    tc = ToolCall(id=event.id, name=event.name, arguments=event.arguments)
                    tool_calls.append(tc)
                elif isinstance(event, Usage):
                    usage = event
                elif isinstance(event, StreamDone):
                    if event.stop_reason == "length":
                        yield TurnFailed("LLM response truncated (max tokens)")
                        return
                    usage = event.usage

            assistant_msg = Message.assistant(
                text="".join(content_parts),
                tool_calls=tuple(tool_calls),
                usage=usage,
            )

            if usage:
                self.budget.consume(
                    tokens=usage.input_tokens + usage.output_tokens,
                    cost_usd=usage.cost_usd,
                )

            messages.append(assistant_msg)

            if self.store is not None:
                await self.store.append(self.session_id, assistant_msg)

            if not tool_calls:
                if self.event_bus:
                    await self.event_bus.emit(
                        "turn_complete", self.session_id, assistant_msg.content
                    )
                if self.store is not None:
                    await self.store.append(self.session_id, messages[-1])
                if self._extractor is not None:
                    try:
                        history = tuple(
                            {"role": m.role, "content": m.content}
                            for m in messages[-10:]
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
                msg = Message.tool_result(result, tool_call_id=tc.id)
                messages.append(msg)
                if self.store is not None:
                    await self.store.append(self.session_id, msg)
                yield ToolResultDelta(
                    id=tc.id, name=tc.name,
                    content=result.content[:200],
                    is_error=result.is_error,
                )

        yield TurnFailed(f"budget exhausted after {self.budget.max_iterations} iterations")

    async def _run_tool_calls(
        self, calls: list[ToolCall]
    ) -> tuple[list[ToolResult], list[ToolProgressDelta]]:
        """Execute tool calls concurrently, collecting progress events."""
        results: list[ToolResult | None] = [None] * len(calls)
        progress_events: list[ToolProgressDelta] = []

        async def _settle(idx: int, call: ToolCall) -> None:
            try:
                from ..tools.builtins import task as _task_module
                _task_module._current_runner.set(self)

                modified = call
                for hook in self.tool_hooks:
                    modified = await hook.before(modified, None)
                    if modified is None:
                        results[idx] = ToolResult.denied("blocked by tool hook")
                        return
                    call = modified

                # Try streaming, fall back to regular execution
                try:
                    from ..core.tool import ToolProgressDelta
                    async for event in self.registry.execute_stream(call):
                        if isinstance(event, ToolProgressDelta):
                            progress_events.append(event)
                        elif isinstance(event, ToolResult):
                            result = event
                            break
                    else:
                        result = ToolResult.ok("(no output)")
                except (TypeError, AttributeError):
                    # execute_stream not available — fall back to execute
                    result = await self.registry.execute(call)

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
