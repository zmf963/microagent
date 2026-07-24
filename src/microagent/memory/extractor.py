"""MemoryExtractor — LLM-based memory extraction from conversations.

Analyzes conversation history and extracts structured facts and
preferences as Memory objects. Uses fire-and-forget pattern:
extraction runs in the background, results are written to
SQLiteMemoryProvider on completion.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore

from .provider import Memory, MemoryProvider

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Extract key facts and user preferences from this conversation.

For each item, output ONE line in this exact format:
  <fact or preference content here> (category)

Where category is one of: fact, preference, task

Examples:
  User uses Python 3.14. (fact)
  User prefers concise responses. (preference)
  User is working on a MicroAgent project. (task)

Only output lines with (category) — no other text.

Conversation:
{history}"""


class MemoryExtractor:
    """Extracts structured memories from conversations using an LLM.

    The extraction runs as a fire-and-forget background task:
    - sync_turn() schedules extraction but does not block
    - Results are written directly to the MemoryProvider
    - Failures are logged at debug level
    - Pending tasks are tracked so they survive GC
    """

    def __init__(
        self,
        provider: MemoryProvider,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o-mini",
    ):
        self._provider = provider
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._pending: set[asyncio.Task] = set()
        self._client = None

    async def extract_async(self, history: tuple[dict[str, str], ...]) -> None:
        """Run extraction in the background (fire-and-forget).

        Task reference is tracked to prevent GC of in-flight extractions.
        """
        task = asyncio.create_task(self._extract(history))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _extract(self, history: tuple[dict[str, str], ...]) -> None:
        """Internal: call LLM, parse response, write memories."""
        if AsyncOpenAI is None:
            return  # openai not installed

        try:
            if self._client is None:
                self._client = AsyncOpenAI(
                    base_url=self._base_url,
                    api_key=self._api_key,
                )
            prompt = self._build_prompt(history)
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.3,
            )
            text = response.choices[0].message.content or ""
            memories = await self._parse_llm_response(text)
            if memories:
                await self._provider.batch_write(memories)
        except Exception as e:
            logger.debug("Memory extraction failed: %s", e)

    @staticmethod
    def _build_prompt(history: tuple[dict[str, str], ...]) -> str:
        """Build the extraction prompt from conversation history."""
        lines = []
        for msg in history[-10:]:  # last 10 messages
            role = msg.get("role", "user")
            content = msg.get("content", "")
            lines.append(f"{role}: {content}")
        return EXTRACTION_PROMPT.format(history="\n".join(lines))

    @staticmethod
    async def _parse_llm_response(text: str) -> tuple[Memory, ...]:
        """Parse LLM extraction output into Memory objects."""
        if not text.strip():
            return ()

        memories: list[Memory] = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # Look for "(category)" at the end
            idx = line.rfind("(")
            if idx == -1 or not line.endswith(")"):
                continue
            content = line[:idx].strip().rstrip(".")
            category = line[idx + 1 : -1].strip().lower()

            if category in ("fact", "preference", "task"):
                memories.append(
                    Memory(
                        id=f"extract-{uuid.uuid4().hex[:12]}",
                        content=content,
                        category=category,
                        created_at=time.time(),
                    )
                )

        return tuple(memories)
