# Use Pydantic v2 as required dependency for tool schema inference

We use Pydantic v2's `create_model()` + `model_json_schema()` to infer JSON
Schema from `@tool` decorated function signatures, similar to FastAPI's approach.

This makes `pydantic>=2.0,<3.0` a required dependency from M0a, not optional.
Combined with ADR-0001 (openai SDK), the "zero-dependency core" principle from
the design doc is revised: the core has two required third-party dependencies
(`openai` + `pydantic`), both of which are high-quality, widely-adopted libraries.

Rationale: hand-writing `inspect.signature()` → JSON Schema mapping is ~50 lines
but fragile (Union types, Optional, nested models, descriptions all need special
handling). Pydantic handles all of these natively and produces OpenAI-compatible
schemas directly.
