"""SessionRunner — the core conversation loop.

Consumes an LLMClient and a ToolRegistry, runs the tool-use loop:

    while not budget.exhausted:
        1. Call LLM (stream)
        2. If tool_calls → execute → append results → loop
        3. If text → yield TurnComplete → return
"""

from __future__ import annotations

from typing import AsyncIterator

import anyio

from ..core.types import (
    Message, ToolCall, ToolResult,
    TextDelta, ToolCallDelta, ToolResultDelta, TurnComplete, TurnFailed,
    Event, Usage,
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

    async def resume(
        self, session_id: str, store: Store
    ) -> tuple[Message, ...]:
        """Resume a session: load conversation history from store."""
        return tuple(await store.load_history(session_id))

    async def run_turn(
        self,
        messages: list[Message],
    ) -> AsyncIterator[Event]:
        """Run a conversation turn.

        ``messages`` is mutated in-place: the user message should already
        be appended before calling this. Tool results and assistant
        messages are appended as the loop runs.

        Yields TextDelta (streaming), ToolCallDelta (after tool starts),
        and finally TurnComplete or TurnFailed.
        """
        # Auto-save user message (the last message in the list)
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

            # --- 1. Compress if needed + apply context sources + pre_llm_hooks ---
            system = self.system_prompt

            # Check if we need compression (before calling LLM)
            if len(messages) > 10:
                from .compress import count_tokens, compress_with_llm
                if count_tokens(tuple(messages)) > 80_000:
                    messages_list = await compress_with_llm(
                        tuple(messages), self.llm, max_tokens=80_000
                    )
                    messages[:] = list(messages_list)

            for src in self.context_sources:
                system += await src.contribute(None)

            for hook in self.pre_llm_hooks:
                system = await hook(system)

            # --- 2. Call LLM (stream) ---
            oai_tools = self.registry.to_openai_tools() or None

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

            # Build assistant message
            assistant_msg = Message.assistant(
                text="".join(content_parts),
                tool_calls=tuple(tool_calls),
                usage=usage,
            )

            # Consume token/cost budget
            if usage:
                self.budget.consume(
                    tokens=usage.input_tokens + usage.output_tokens,
                    cost_usd=usage.cost_usd,
                )

            messages.append(assistant_msg)

            # Auto-save assistant message (even with tool calls)
            if self.store is not None:
                await self.store.append(self.session_id, assistant_msg)

            # --- 3. If no tool calls → done ---
            if not tool_calls:
                if self.event_bus:
                    await self.event_bus.emit(
                        "turn_complete", self.session_id, assistant_msg.content
                    )
                # Auto-save to store
                if self.store is not None:
                    await self.store.append(self.session_id, messages[-1])  # assistant msg
                yield TurnComplete(assistant_msg.content)
                return

            # --- 4. Execute tool calls concurrently ---
            results = await self._run_tool_calls(tool_calls)

            for tc, result in zip(tool_calls, results):
                msg = Message.tool_result(result, tool_call_id=tc.id)
                messages.append(msg)
                # Auto-save tool result
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
    ) -> list[ToolResult]:
        """Execute tool calls concurrently with anyio TaskGroup.

        Tool exceptions are internalised — one failing tool does not
        cancel others. Only CancelledError propagates.
        """
        results: list[ToolResult | None] = [None] * len(calls)

        async def _settle(idx: int, call: ToolCall) -> None:
            try:
                # Inject current runner for task tool
                from ..tools.builtins import task as _task_module
                _task_module._current_runner = self

                # Run tool hooks: before → execute → after
                modified = call
                for hook in self.tool_hooks:
                    modified = await hook.before(modified, None)
                    if modified is None:
                        results[idx] = ToolResult.denied("blocked by tool hook")
                        return
                    call = modified

                result = await self.registry.execute(call)

                for hook in self.tool_hooks:
                    result = await hook.after(call, result, None)

                results[idx] = result
            except Exception as e:
                results[idx] = ToolResult.error(f"{call.name} failed: {e!r}")

        async with anyio.create_task_group() as tg:
            for idx, call in enumerate(calls):
                tg.start_soon(_settle, idx, call)

        return [
            r if r is not None
            else ToolResult.error("not executed (cancelled)")
            for r in results
        ]
