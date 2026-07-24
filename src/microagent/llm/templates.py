"""Model-specific system prompt templates.

Three templates for DeepSeek-V4, GLM-5.2, and Kimi K3.
Other models use the default template.
"""

from __future__ import annotations

DEFAULT_TEMPLATE = "You are a helpful assistant."

MODEL_TEMPLATES: dict[str, str] = {
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


def get_model_template(model: str) -> str:
    """Get the system prompt template for a model by prefix match.

    Falls back to DEFAULT_TEMPLATE for unknown models.
    """
    model_lower = model.lower()
    for prefix, template in MODEL_TEMPLATES.items():
        if model_lower.startswith(prefix):
            return template
    return DEFAULT_TEMPLATE
