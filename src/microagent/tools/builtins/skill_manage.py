"""skill_manage builtin tool — runtime Skill creation/modification/deletion.

Agent can create, patch, list, and delete Skills at runtime.
This is the core mechanism of the self-improving learning loop (§7.5).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...core.tool import tool
from ...core.types import ToolResult
from ..safe_id import is_safe_name


def _get_skills_dir() -> Path:
    return Path.home() / ".microagent" / "skills"


def _provenance_file(skill_dir: Path) -> Path:
    return skill_dir / ".provenance.json"


def _record_provenance(name: str, created_by: str = "agent") -> None:
    skills_dir = _get_skills_dir()
    pf = _provenance_file(skills_dir / name)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"created_by": created_by}))


def _is_agent_created(name: str) -> bool:
    pf = _provenance_file(_get_skills_dir() / name)
    if not pf.exists():
        return False
    try:
        data = json.loads(pf.read_text())
        return data.get("created_by") == "agent"
    except (json.JSONDecodeError, KeyError):
        return False


def _touch_curator_usage(name: str) -> None:
    """Update curator usage tracking for a skill (last_activity timestamp)."""
    import time

    usage_file = _get_skills_dir() / ".usage.json"
    now = time.time()
    data = {}
    if usage_file.exists():
        try:
            data = json.loads(usage_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    entry = data.get(name, {})
    entry["last_activity"] = now
    entry["use_count"] = entry.get("use_count", 0) + 1
    if "state" not in entry:
        entry["state"] = "active"
    data[name] = entry
    try:
        usage_file.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


@tool("skill_manage", description="Create, patch, list, or delete Skills at runtime.")
async def skill_manage(
    action: Annotated[str, Field(description="Action: create | patch | list | delete")],
    name: Annotated[str, Field(description="Skill name")] = "",
    content: Annotated[str, Field(description="SKILL.md content (for create)")] = "",
    old_string: Annotated[str, Field(description="Text to find (for patch)")] = "",
    new_string: Annotated[str, Field(description="Replacement text (for patch)")] = "",
) -> ToolResult:
    skills_dir = _get_skills_dir()

    if action == "create":
        if not name or not content:
            return ToolResult.error("name and content are required for create")
        if not is_safe_name(name):
            return ToolResult.error(
                "invalid skill name: must be alphanumeric with optional "
                "'_', '-', '.' — no path separators or '..'"
            )

        MAX_SKILL_SIZE = 1_000_000  # 1 MB
        if len(content) > MAX_SKILL_SIZE:
            return ToolResult.error(
                f"skill content too large: {len(content)} bytes exceeds {MAX_SKILL_SIZE} limit"
            )

        skill_path = skills_dir / name / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        import asyncio

        await asyncio.to_thread(skill_path.write_text, content)
        _record_provenance(name, created_by="agent")
        # Touch the curator usage file so the new skill is tracked
        _touch_curator_usage(name)
        return ToolResult.ok(f"Skill '{name}' created at {skill_path}")

    elif action == "patch":
        if not name or not old_string:
            return ToolResult.error("name and old_string are required for patch")
        if not is_safe_name(name):
            return ToolResult.error("invalid skill name")
        skill_path = skills_dir / name / "SKILL.md"
        if not skill_path.exists():
            return ToolResult.error(f"Skill '{name}' not found")

        text = skill_path.read_text()
        count = text.count(old_string)
        if count == 0:
            return ToolResult.error(f"old_string not found in skill '{name}'")
        # Require a unique match unless caller explicitly opts into
        # multi-replace. Previously the guard was `count > 1 and new_string`
        # — an empty new_string (deletion) bypassed it, silently removing
        # ALL occurrences when the user intended one.
        if count > 1:
            return ToolResult.error(
                f"old_string matches {count} times in skill '{name}'. "
                f"Make old_string more specific to target exactly one occurrence."
            )
        new_text = text.replace(old_string, new_string)
        skill_path.write_text(new_text)
        return ToolResult.ok(f"Skill '{name}' patched")

    elif action == "list":
        if not skills_dir.exists():
            return ToolResult.ok("(no skills)")
        agent_skills = []
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and _is_agent_created(d.name):
                agent_skills.append(d.name)
        if not agent_skills:
            return ToolResult.ok("(no agent-created skills)")
        return ToolResult.ok(
            "agent-created skills:\n" + "\n".join(f"  - {s}" for s in agent_skills)
        )

    elif action == "delete":
        if not name:
            return ToolResult.error("name is required for delete")
        if not is_safe_name(name):
            return ToolResult.error("invalid skill name")
        skill_dir = skills_dir / name
        if not skill_dir.exists():
            return ToolResult.error(f"Skill '{name}' not found")
        if not _is_agent_created(name):
            return ToolResult.error(
                f"Skill '{name}' was not created by an agent. "
                f"Only agent-created skills can be deleted via skill_manage."
            )
        shutil.rmtree(skill_dir)
        return ToolResult.ok(f"Skill '{name}' deleted")

    else:
        return ToolResult.error(f"unknown action: {action}. Use: create, patch, list, delete")
