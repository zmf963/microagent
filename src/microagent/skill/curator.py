"""Curator — background skill lifecycle management.

Tracks agent-created skill usage and auto-archives stale skills.
Design principles (from design doc §7.5.2):
- Only touches skills with created_by="agent" provenance
- Never deletes — archives to .archive/ (restorable)
- Pinned skills are exempt from all auto-transitions
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SkillUsage:
    """Tracked usage stats for a single skill."""
    name: str
    use_count: int
    last_activity: float
    state: str           # "active" | "stale" | "archived"
    pinned: bool = False


class Curator:
    """Background skill lifecycle manager.

    Scans agent-created skills periodically and auto-transitions:
    - active → stale (idle > stale_after_days)
    - stale → archived (idle > archive_after_days, moved to .archive/)
    - Pinned skills are always skipped.
    """

    def __init__(
        self,
        stale_after_days: float = 30.0,
        archive_after_days: float = 90.0,
    ):
        self.stale_after_days = stale_after_days
        self.archive_after_days = archive_after_days

    async def run_once(self, skills_dir: Path, usage_file: Path) -> None:
        """Single scan: read usage.json → update states → execute transitions."""
        usage = self._load_usage(usage_file)
        now = time.time()

        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            if not self._is_agent_created(skill_dir):
                continue

            entry = usage.get(skill_dir.name)
            if entry is None or entry.get("pinned"):
                continue

            days_idle = (now - entry.get("last_activity", now)) / 86400

            if days_idle > self.archive_after_days and entry.get("state") == "stale":
                self._archive(skill_dir)
                entry["state"] = "archived"

            elif days_idle > self.stale_after_days and entry.get("state") == "active":
                entry["state"] = "stale"

        self._save_usage(usage_file, usage)

    @staticmethod
    def _is_agent_created(skill_dir: Path) -> bool:
        pf = skill_dir / ".provenance.json"
        if not pf.exists():
            return False
        try:
            data = json.loads(pf.read_text())
            return data.get("created_by") == "agent"
        except (json.JSONDecodeError, KeyError):
            return False

    @staticmethod
    def _archive(skill_dir: Path) -> None:
        archive = skill_dir.parent / ".archive"
        archive.mkdir(exist_ok=True)
        dest = archive / skill_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        skill_dir.rename(dest)

    @staticmethod
    def _load_usage(usage_file: Path) -> dict:
        if not usage_file.exists():
            return {}
        return json.loads(usage_file.read_text())

    @staticmethod
    def _save_usage(usage_file: Path, data: dict) -> None:
        usage_file.parent.mkdir(parents=True, exist_ok=True)
        usage_file.write_text(json.dumps(data, indent=2))
