"""Cron scheduler — run agent prompts on a schedule.

Uses APScheduler (AsyncIOScheduler) for cron/interval triggering.
The agent prompt runs asynchronously; results are logged but not
delivered (delivery is a future feature).

v1.0: fcntl file lock for cross-process safety + result persistence.
"""

from __future__ import annotations

import fcntl
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-process file lock
# ---------------------------------------------------------------------------


def _try_acquire_lock(lock_file: Path, return_fd: bool = False):
    """Try to acquire an exclusive lock on lock_file.

    Returns True if acquired (or fd if return_fd=True), False/None if locked.
    Uses fcntl.flock(LOCK_EX | LOCK_NB) for non-blocking acquire.
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = open(lock_file, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if return_fd:
            return fd
        return True
    except (OSError, IOError):
        if return_fd:
            return None
        return False


def _release_lock(fd) -> None:
    """Release the file lock."""
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------


def _save_cron_output(base_dir: Path, job_id: str, prompt: str, response: str) -> str:
    """Save cron job output to disk.

    Returns the file path. Format: {base_dir}/{job_id}/{timestamp}.md
    """
    out_dir = base_dir / "output" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time())
    filename = f"{timestamp}.md"
    filepath = out_dir / filename

    content = (
        f"# Cron Job: {job_id}\n\n"
        f"**Timestamp:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))}\n\n"
        f"## Prompt\n\n{prompt}\n\n"
        f"## Response\n\n{response}\n"
    )
    filepath.write_text(content)
    return str(filepath)


@dataclass(frozen=True, slots=True)
class CronJob:
    """A scheduled agent task."""

    name: str
    schedule: str  # cron expression or "interval:N"
    prompt: str  # prompt fed to the agent
    session_strategy: Literal["new", "resume:last"] = "new"
    enabled: bool = True


@dataclass
class CronScheduler:
    """Manages scheduled jobs using APScheduler.

    v1.0: Uses fcntl file lock to prevent multiple gateway instances
    from running the same cron job concurrently.
    """

    agent: object  # microagent.Agent (avoid circular import)
    store: object = None  # Store | None — needed for session_strategy="resume:last"
    jobs: dict[str, CronJob] = field(default_factory=dict)
    lock_path: str = ""  # override default lock file path

    def __post_init__(self):
        self._scheduler = None
        self._started = False
        self._lock_fd = None
        if not self.lock_path:
            self.lock_path = str(Path.home() / ".microagent" / "cron.lock")

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
        """Start the scheduler. Acquires cross-process lock first.

        If another process holds the lock, logs a warning and returns
        without starting (single-writer pattern).
        """
        lock_file = Path(self.lock_path)
        self._lock_fd = _try_acquire_lock(lock_file, return_fd=True)
        if self._lock_fd is None:
            logger.warning("Cron lock held by another process — not starting")
            return

        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._scheduler = AsyncIOScheduler()
        self._started = True

        for job in self.jobs.values():
            if job.enabled:
                self._schedule_job(job)

        self._scheduler.start()

    async def stop(self) -> None:
        """Stop the scheduler gracefully and release the lock."""
        self._started = False
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
        if self._lock_fd is not None:
            _release_lock(self._lock_fd)
            self._lock_fd = None

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

        v1.0: Persists output to ~/.microagent/cron/output/{job_id}/{ts}.md
        """
        try:
            from ..core.types import Message

            if job.session_strategy == "resume:last" and self.store is not None:
                messages = await self._build_resume_messages(job)
            else:
                messages = [Message.user(job.prompt)]

            result = await self.agent.arun(messages)
            logger.info(f"Cron job '{job.name}' completed: {result[:200]}")

            # Persist result
            try:
                base = Path.home() / ".microagent" / "cron"
                _save_cron_output(base, job.name, job.prompt, result)
            except Exception as e:
                logger.warning(f"Failed to persist cron output: {e}")

        except Exception as e:
            logger.error(f"Cron job '{job.name}' failed: {e}")

    async def _build_resume_messages(self, job: CronJob) -> list:
        """Build messages for resume:last strategy — load last session + append prompt."""
        from ..core.types import Message

        try:
            sessions = await self.store.list_sessions()
            if not sessions:
                return [Message.user(job.prompt)]

            # Pick the most recent session (list_sessions returns DESC by recency)
            last_sid = sessions[0]
            history = await self.store.load_history(last_sid)
            if not history:
                return [Message.user(job.prompt)]

            # Append job prompt as a new user message to the continued conversation
            return list(history) + [Message.user(job.prompt)]
        except Exception:
            # Store failure — fall back to fresh conversation
            return [Message.user(job.prompt)]
