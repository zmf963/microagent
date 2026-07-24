"""Cron scheduler — run agent prompts on a schedule.

Uses APScheduler (AsyncIOScheduler) for cron/interval triggering.
The agent prompt runs asynchronously; results are logged but not
delivered (delivery is a future feature).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CronJob:
    """A scheduled agent task."""
    name: str
    schedule: str           # cron expression or "interval:N"
    prompt: str             # prompt fed to the agent
    session_strategy: Literal["new", "resume:last"] = "new"
    enabled: bool = True


@dataclass
class CronScheduler:
    """Manages scheduled jobs using APScheduler."""

    agent: object  # microagent.Agent (avoid circular import)
    store: object = None  # Store | None — needed for session_strategy="resume:last"
    jobs: dict[str, CronJob] = field(default_factory=dict)

    def __post_init__(self):
        self._scheduler = None
        self._started = False

    def add_job(self, job: CronJob) -> None:
        """Add a job. If scheduler is running, schedule immediately."""
        self.jobs[job.name] = job
        if self._started and self._scheduler is not None and job.enabled:
            self._schedule_job(job)

    def remove_job(self, name: str) -> None:
        """Remove a job by name."""
        self.jobs.pop(name, None)
        if self._scheduler is not None:
            try:
                self._scheduler.remove_job(name)
            except Exception:
                pass

    def start(self) -> None:
        """Start the scheduler. Schedules all enabled jobs."""
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        self._scheduler = AsyncIOScheduler()
        self._started = True

        for job in self.jobs.values():
            if job.enabled:
                self._schedule_job(job)

        self._scheduler.start()

    async def stop(self) -> None:
        """Stop the scheduler gracefully."""
        self._started = False
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)

    def _schedule_job(self, job: CronJob) -> None:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        if job.schedule.startswith("interval:"):
            seconds = int(job.schedule.split(":")[1])
            trigger = IntervalTrigger(seconds=seconds)
        else:
            trigger = CronTrigger.from_crontab(job.schedule)

        self._scheduler.add_job(
            self._execute_job,
            trigger=trigger,
            args=[job],
            id=job.name,
            replace_existing=True,
        )

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a scheduled job: run the agent with the prompt.

        Supports session_strategy:
          - "new": fresh conversation each tick (default)
          - "resume:last": load the last session's history from store,
            append the job prompt as a new user message, and continue.
        """
        try:
            from ..core.types import Message

            if job.session_strategy == "resume:last" and self.store is not None:
                messages = await self._build_resume_messages(job)
            else:
                messages = [Message.user(job.prompt)]

            result = await self.agent.arun(messages)
            logger.info(f"Cron job '{job.name}' completed: {result[:200]}")
        except Exception as e:
            logger.error(f"Cron job '{job.name}' failed: {e}")

    async def _build_resume_messages(self, job: CronJob) -> list:
        """Build messages for resume:last strategy — load last session + append prompt."""
        from ..core.types import Message

        try:
            sessions = await self.store.list_sessions()
            if not sessions:
                return [Message.user(job.prompt)]

            # Pick the most recent session
            last_sid = sessions[-1]
            history = await self.store.load_history(last_sid)
            if not history:
                return [Message.user(job.prompt)]

            # Append job prompt as a new user message to the continued conversation
            return list(history) + [Message.user(job.prompt)]
        except Exception:
            # Store failure — fall back to fresh conversation
            return [Message.user(job.prompt)]
