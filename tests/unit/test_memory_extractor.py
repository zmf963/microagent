"""Tests for MemoryExtractor — LLM-based memory extraction from conversations."""

from microagent.memory.extractor import MemoryExtractor


class TestMemoryExtractor:
    async def test_extract_facts_from_history(self):
        """Extract structured memories from conversation history."""
        history = (
            {"role": "user", "content": "I prefer Python over JavaScript."},
            {"role": "assistant", "content": "Noted! I'll use Python when possible."},
            {"role": "user", "content": "My project is in /home/user/myapp."},
            {"role": "assistant", "content": "Got it. I'll check that directory for files."},
        )

        memories = await MemoryExtractor._parse_llm_response(
            """
User prefers Python over JavaScript. (preference)
User project is at /home/user/myapp. (fact)
        """.strip()
        )

        assert len(memories) == 2
        assert memories[0].category == "preference"
        assert "Python" in memories[0].content
        assert memories[1].category == "fact"
        assert "myapp" in memories[1].content

    async def test_parse_empty_response(self):
        """Empty LLM response → no memories."""
        memories = await MemoryExtractor._parse_llm_response("")
        assert memories == ()

    async def test_parse_malformed_response(self):
        """LLM responses without category markers are skipped."""
        memories = await MemoryExtractor._parse_llm_response(
            "This is just a sentence without a category."
        )
        assert memories == ()

    def test_extract_prompt_includes_history(self):
        """The extraction prompt references the conversation."""
        history = ({"role": "user", "content": "I like cats."},)
        prompt = MemoryExtractor._build_prompt(history)
        assert "I like cats" in prompt
        assert "fact" in prompt.lower() or "preference" in prompt.lower()


class TestParseMore:
    async def test_parse_invalid_category_skipped(self):
        memories = await MemoryExtractor._parse_llm_response(
            "Some fact. (unknown-category)\n"
            "A preference. (preference)\n"
            "No category here"
        )
        # Only the valid preference line is kept; unknown category skipped
        assert len(memories) == 1
        assert memories[0].category == "preference"

    async def test_parse_strips_trailing_period(self):
        memories = await MemoryExtractor._parse_llm_response(
            "User likes coffee. (preference)"
        )
        assert len(memories) == 1
        # trailing period stripped from content
        assert not memories[0].content.endswith(".")

    async def test_parse_whitespace_only_lines(self):
        memories = await MemoryExtractor._parse_llm_response(
            "\n  \nFact one. (fact)\n  \n"
        )
        assert len(memories) == 1

    async def test_parse_unclosed_paren(self):
        memories = await MemoryExtractor._parse_llm_response("Fact (fact")
        assert memories == ()


class TestLifecycle:
    async def test_close_with_no_client(self):
        extractor = MemoryExtractor(provider=None, base_url="http://x", api_key="k", model="m")
        await extractor.close()  # must not crash (no client, no pending)

    async def test_close_cancels_pending(self):
        extractor = MemoryExtractor(provider=None, base_url="http://x", api_key="k", model="m")
        # Schedule a fake extract task that never completes
        import asyncio

        async def _slow():
            await asyncio.sleep(10)

        task = asyncio.create_task(_slow())
        extractor._pending.add(task)
        await extractor.close()
        assert task.cancelled()

    async def test_extract_returns_early_when_no_openai(self, monkeypatch):
        # Simulate AsyncOpenAI being None
        import microagent.memory.extractor as ext
        monkeypatch.setattr(ext, "AsyncOpenAI", None)
        extractor = MemoryExtractor(provider=None, base_url="http://x", api_key="k", model="m")
        result = await extractor._extract(({"role": "user", "content": "hi"},))
        assert result is None

    async def test_extract_returns_early_when_closed(self, monkeypatch):
        import microagent.memory.extractor as ext
        # Pretend openai is available
        class FakeOpenAI:
            def __init__(self, **kw): pass
        monkeypatch.setattr(ext, "AsyncOpenAI", FakeOpenAI)
        extractor = MemoryExtractor(provider=None, base_url="http://x", api_key="k", model="m")
        extractor._closed = True
        result = await extractor._extract(({"role": "user", "content": "hi"},))
        assert result is None

    async def test_extract_error_is_logged(self, caplog, monkeypatch):
        import logging
        import microagent.memory.extractor as ext

        class FakeOpenAI:
            def __init__(self, **kw): pass

        class _Completions:
            async def create(self, **kw):
                raise RuntimeError("network down")

        class _Chat:
            def __init__(self):
                self.completions = _Completions()

        class _Client:
            def __init__(self):
                self.chat = _Chat()
            async def close(self): pass

        monkeypatch.setattr(ext, "AsyncOpenAI", FakeOpenAI)

        class FakeProvider:
            async def batch_write(self, memories): pass

        extractor = MemoryExtractor(provider=FakeProvider(), base_url="http://x", api_key="k", model="m")
        extractor._client = _Client()
        with caplog.at_level(logging.DEBUG):
            await extractor._extract(({"role": "user", "content": "hi"},))
        # error logged at debug, not raised
        assert any("Memory extraction failed" in r.message for r in caplog.records)


class TestBuildPromptBounds:
    def test_prompt_is_bounded(self):
        """Full tool results (~50KB each) must not flow unbounded into the
        extraction prompt every turn."""
        from microagent.memory.extractor import MemoryExtractor

        history = tuple(
            {"role": "tool", "content": "y" * 50_000} for _ in range(10)
        )
        prompt = MemoryExtractor._build_prompt(history)
        assert len(prompt) < 25_000

    def test_prompt_keeps_small_messages(self):
        from microagent.memory.extractor import MemoryExtractor

        history = (
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        )
        prompt = MemoryExtractor._build_prompt(history)
        assert "hello" in prompt and "world" in prompt
