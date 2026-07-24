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
