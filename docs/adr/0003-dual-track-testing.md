# Dual-track testing: mock (unit) + real API (integration)

Tests are split into two categories, both run automatically via pytest:

## Unit tests (default, no marker needed)

- Use `FakeLLMClient` implementing the `LLMClient` Protocol to inject
  deterministic StreamEvent sequences. No network calls.
- Covers: types, tool registry, schema inference, SessionRunner loop logic,
  budget exhaustion, tool execution, error handling.
- Run with: `pytest` (default), `pytest tests/unit/`

## Integration tests (marker: `@pytest.mark.integration`)

- Use a real OpenAI-compatible API endpoint to verify the full pipeline:
  SSE parsing, tool_call delta accumulation, real tool execution, multi-turn.
- Endpoint config (via env vars, not committed to repo):
  - `MICROAGENT_TEST_BASE_URL=http://10.144.0.2:20128/v1`
  - `MICROAGENT_TEST_API_KEY=sk-...`
  - `MICROAGENT_TEST_MODEL=oc-d4f`
- Run with: `pytest -m integration` or `pytest tests/integration/`
- Skipped automatically if env vars are not set (no network / no key).

## pytest configuration

- `pytest-asyncio` with `asyncio_mode = "auto"`
- Custom markers: `integration`
- Default `pytest` run = unit only (fast, hermetic)
- `pytest -m integration` = integration only
- `pytest -m "" = all` (unit + integration)
