"""Model-specific system prompt templates.

Three templates for DeepSeek-V4, GLM-5.2, and Kimi K3.
Other models use the default template.
"""

from __future__ import annotations

# Single source of truth for the generic default prompt.
# config.py and agent.py import this instead of hardcoding their own.
DEFAULT_TEMPLATE = "You are a helpful assistant."

MODEL_TEMPLATES: dict[str, str] = {
    "deepseek-v4-flash": (
        "You are a helpful assistant powered by DeepSeek-V4 Flash, a fast, "
        "low-latency reasoning model. You prioritize concise, correct, "
        "direct responses. For code: give complete runnable solutions, but "
        "avoid unnecessary verbosity. Use markdown code blocks with "
        "language specifiers. Match the user's language when it is Chinese "
        "or English."
    ),
    "deepseek-v4": (
        "You are a helpful assistant powered by DeepSeek-V4. "
        "You excel at code generation, analysis, and step-by-step reasoning. "
        "When writing code, prefer clarity and correctness over brevity. "
        "Use markdown code blocks with language specifiers."
    ),
    "glm-5.2": (
        "You are a helpful assistant powered by GLM-5.2. "
        "You are proficient in both Chinese and English. "
        "When responding, match the language of the user's query. "
        "For code tasks, provide complete and runnable solutions."
    ),
    "kimi-k3": (
        "You are a helpful assistant powered by Kimi K3. "
        "You specialize in long-context understanding and document analysis. "
        "When processing large documents, summarize key points before diving into details. "
        "For coding tasks, provide explanations alongside the code."
    ),
}

# Gateway alias → canonical model-family prefix. Local gateways expose
# DeepSeek-V4 variants under compact aliases that don't match the
# "deepseek-v4*" prefixes the template table uses, so lookup would fall
# through to the generic default. Verified against the gateway's own
# /model response:
#   tx-d4f → deepseek-v4-flash  (fast/low-latency variant)
#   oc-d4f → deepseek-v4-flash  (OpenCode gateway alias)
#   tx-d4p → deepseek-v4-pro    (the pro variant — NOT flash)
# (Previously tx-d4p was mapped to flash, which gave pro callers the
# wrong system-prompt guidance.)
_ALIAS_TO_MODEL: dict[str, str] = {
    "tx-d4f": "deepseek-v4-flash",
    "oc-d4f": "deepseek-v4-flash",
    "tx-d4p": "deepseek-v4-pro",
}


def get_model_template(model: str) -> str:
    """Get the system prompt template for a model.

    Gateway aliases (tx-d4f → deepseek-v4-flash, etc.) are resolved first,
    then longest-prefix match wins so "deepseek-v4" doesn't shadow a more
    specific entry like "deepseek-v4-flash".
    Falls back to DEFAULT_TEMPLATE for unknown models.
    """
    model_lower = model.lower()
    # Resolve gateway aliases to the canonical model-family prefix.
    canonical = _ALIAS_TO_MODEL.get(model_lower, model_lower)
    best: tuple[int, str] = (0, DEFAULT_TEMPLATE)
    for prefix, template in MODEL_TEMPLATES.items():
        if canonical.startswith(prefix) and len(prefix) > best[0]:
            best = (len(prefix), template)
    return best[1]


def build_system_prompt(model: str, user_prompt: str = "") -> str:
    """Compose the final system prompt: user custom + model template.

    User's custom prompt always comes first (higher priority instructions).
    Model template is appended as supplementary capability guidance —
    it is NOT discarded just because the user set a custom prompt.
    """
    user_prompt = (user_prompt or "").strip()
    model_tmpl = get_model_template(model)

    if not user_prompt or user_prompt == DEFAULT_TEMPLATE:
        # No meaningful user prompt — model template alone
        return model_tmpl

    if model_tmpl == DEFAULT_TEMPLATE:
        # Unknown model — user prompt alone, no generic filler
        return user_prompt

    # Both: user instructions take precedence, model guidance appended
    return f"{user_prompt}\n\n{model_tmpl}"
