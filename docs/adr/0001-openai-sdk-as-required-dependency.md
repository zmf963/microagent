# Use openai SDK v2 as required dependency for LLM calls

We use the official `openai` Python SDK v2 (`AsyncOpenAI`) for all LLM API calls,
including calls to non-OpenAI compatible endpoints (vLLM, Ollama, etc.).

This overrides the design doc's "zero-dependency core" principle: `openai>=2.0,<3.0`
is a required dependency, not an optional extra. The SDK handles SSE parsing,
tool-call delta accumulation, retries, and type safety — all of which are
bug-prone to reimplement with raw httpx.

The design doc's claim that "one OpenAIChatClient + different base_url covers
all endpoints" still holds — the openai SDK supports `base_url` override natively.
