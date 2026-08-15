"""Learner — /learn support: distill a skill from chat/dir/url (Hermes parity).

Hermes design: skill creation is a DELIBERATE act (``/learn``), not an
automatic background loop — routine background work must cost zero
tokens. This module implements the one-shot distillation call: feed the
source (conversation text, directory tree, or fetched URL) to the LLM
(auxiliary model when configured), and write the generated SKILL.md into
~/.microagent/skills with agent provenance + curator usage tracking
(same write path as the skill_manage tool, so the Curator will manage
the new skill's lifecycle).
"""

from __future__ import annotations

import json
from pathlib import Path

from .loader import ClaudeSkillLoader

LEARN_PROMPT = """You are creating a reusable skill from the following material.

Write a complete SKILL.md in the standard skill format:

---
name: <kebab-case name>
description: <one sentence; when to use this skill, with trigger words>
---

# <Title>

<Concrete steps, commands, gotchas, and exact file paths from the material.
Keep it actionable — someone reading this later must be able to execute
without re-deriving anything. Include any credentials ONLY if they were
explicitly marked shareable.>

Material:
{material}

Respond with ONLY the SKILL.md content (starting with ---)."""


async def learn_skill(
    source: str,
    *,
    kind: str = "chat",
    llm: object | None = None,
    skills_dir: Path | None = None,
) -> str:
    """Distill a skill from source material and write it to disk.

    Returns a status string. Never raises — errors are reported in the
    returned string (the /learn command surfaces them).
    """
    skills_dir = skills_dir or (Path.home() / ".microagent" / "skills")

    try:
        material = await _collect_material(source, kind)
    except Exception as e:
        return f"[error] failed to read source: {e!r}"
    if not material.strip():
        return "[error] source material is empty"

    if llm is None:
        return "[error] no LLM configured for skill learning"

    # Prefer the auxiliary model for the distillation call (cheaper).
    try:
        distill_llm = llm
        if getattr(llm.config, "auxiliary_model", None):
            distill_llm = llm.for_model(llm.config.auxiliary_model)
    except Exception:
        distill_llm = llm

    try:
        from ..core.types import Message, TextDelta

        response_text = ""
        async for event in distill_llm.stream(
            system="You are a skill author. Output only the SKILL.md content.",
            messages=(Message.user(LEARN_PROMPT.format(material=material[:30_000])),),
            tools=None,
        ):
            if isinstance(event, TextDelta) and event.kind == "content":
                response_text += event.text
    except Exception as e:
        return f"[error] LLM call failed: {e!r}"

    if not response_text.strip():
        return "[error] LLM returned an empty skill"

    name = _extract_name(response_text)
    if name is None:
        return "[error] generated skill has no valid frontmatter name"

    from ..tools.safe_id import is_safe_name

    if not is_safe_name(name):
        return f"[error] generated skill name {name!r} is not a safe name"

    try:
        _write_skill(skills_dir, name, response_text.strip())
    except Exception as e:
        return f"[error] failed to write skill: {e!r}"

    # Invalidate loader caches so the new skill is matchable immediately.
    try:
        ClaudeSkillLoader.invalidate_all()
    except Exception:
        pass

    return (
        f"Learned skill '{name}' → {skills_dir / name / 'SKILL.md'} "
        f"(provenance: agent; curator will track its lifecycle)"
    )


async def _collect_material(source: str, kind: str) -> str:
    """Gather the material to distill, per kind."""
    if kind == "chat":
        return source

    if kind == "dir":
        p = Path(source).expanduser()
        if not p.is_dir():
            raise ValueError(f"not a directory: {source}")
        lines: list[str] = []
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            if ".git" in f.parts or "__pycache__" in f.parts or f.name.endswith(".pyc"):
                continue
            if f.stat().st_size > 200_000:
                lines.append(f"\n# {f} (truncated)\n")
                continue
            try:
                text = f.read_text(errors="replace")[:20_000]
            except OSError:
                continue
            if len(lines) and len("\n".join(lines)) + len(text) > 80_000:
                lines.append("\n# ... (further files omitted) ...\n")
                break
            lines.append(f"\n# === {f} ===\n{text}")
        return "\n".join(lines)

    if kind == "url":
        from ..tools.builtins.web_fetch import _resolve_and_check

        from urllib.parse import urlparse

        parsed = urlparse(source)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
        import asyncio

        error = await asyncio.to_thread(_resolve_and_check, parsed.hostname or "")
        if error is not None:
            raise ValueError(f"URL blocked by SSRF protection: {error}")
        import httpx

        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
            resp = await client.get(source)
            resp.raise_for_status()
            return resp.text[:50_000]

    raise ValueError(f"unknown kind: {kind!r} (use chat|dir|url)")


def _extract_name(text: str) -> str | None:
    """Extract the name from generated SKILL.md frontmatter."""
    for line in text.splitlines()[:15]:
        stripped = line.strip()
        if stripped.startswith("name:"):
            name = stripped[len("name:"):].strip()
            return name or None
    return None


def _write_skill(skills_dir: Path, name: str, content: str) -> None:
    """Write SKILL.md + provenance + curator usage entry.

    Mirrors skill_manage's create path so both routes produce identical
    on-disk shapes (Curator treats them the same). ALL three artifacts
    (SKILL.md, .provenance.json, .usage.json) land in the CALLER-provided
    skills_dir — provenance/usage previously ignored the parameter and
    wrote to the real ~/.microagent/skills, corrupting the user's curator
    state when a custom dir was passed (e.g. test fixtures).
    """
    from ..tools.builtins.skill_manage import _record_provenance, _touch_curator_usage

    skill_path = skills_dir / name / "SKILL.md"
    if skill_path.exists():
        raise ValueError(f"skill {name!r} already exists — delete it first")
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(content)
    _record_provenance(name, created_by="agent", skills_dir=skills_dir)
    _touch_curator_usage(name, skills_dir=skills_dir)
