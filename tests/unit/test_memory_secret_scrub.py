"""Round-22 🟡: memories persist raw conversation excerpts — an API key
pasted in chat would live in the 500-row LRU memory store and resurface
in every future session's prompt via recall. Extraction output is now
scrubbed before persisting."""

from microagent.memory.extractor import MemoryExtractor
from microagent.security.secrets import scrub_secrets


class TestScrubSecrets:
    def test_openai_style_key(self):
        assert "sk-abc123XYZ456def" not in scrub_secrets(
            "my key is sk-abc123XYZ456def please remember"
        )

    def test_github_tokens(self):
        for tok in ("ghp_" + "a" * 30, "github_pat_11ABC" + "b" * 30):
            assert tok not in scrub_secrets(f"token {tok} ok")

    def test_assignment_values_redacted(self):
        out = scrub_secrets('config api_key: "supersecret123" end')
        assert "supersecret123" not in out
        assert "api_key" in out
        out2 = scrub_secrets("export API_TOKEN=tok_123456 tail")
        assert "tok_123456" not in out2

    def test_aws_google_jwt(self):
        assert "AKIAIOSFODNN7EXAMPLE" not in scrub_secrets(
            "aws AKIAIOSFODNN7EXAMPLE"
        )
        jwt = "eyJ" + "a" * 25 + "." + "b" * 25 + "." + "c" * 15
        assert jwt not in scrub_secrets(f"jwt {jwt}")

    def test_ordinary_prose_untouched(self):
        text = "User prefers Python 3.14 and concise responses. Works on MicroAgent."
        assert scrub_secrets(text) == text

    def test_empty_after_scrub_dropped(self):
        text = "sk-abcdefgh12345678 (fact)"
        memories = []
        import asyncio

        memories = asyncio.run(MemoryExtractor._parse_llm_response(text))
        assert memories == ()  # nothing left worth remembering


class TestExtractorScrubsBeforePersist:
    async def test_extracted_memories_are_clean(self):
        text = (
            "User's gateway key is sk-ZZZZ9999YYYY8888 for deploys. (fact)\n"
            "User likes tea. (preference)\n"
        )
        memories = await MemoryExtractor._parse_llm_response(text)
        assert len(memories) == 2
        assert not any("sk-ZZZZ" in m.content for m in memories)
        assert any("tea" in m.content for m in memories)
