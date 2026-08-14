"""Cron scheduler — run agent prompts on a schedule.

Uses APScheduler (AsyncIOScheduler) for cron/interval triggering.
The agent prompt runs asynchronously; results are logged but not
delivered (delivery is a future feature).

v1.0: fcntl file lock for cross-process safety + result persistence.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# fcntl is POSIX-only. The rest of MicroAgent guards Windows-portable
# modules (readline, termios, tty) with try/except so `import microagent`
# succeeds on Windows; cron/scheduler is imported unconditionally from
# __init__.py, so an unguarded `import fcntl` made the whole package
# fail to import on Windows. Fall back to no-op locking there.
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-process file lock
# ---------------------------------------------------------------------------


def _try_acquire_lock(lock_file: Path, return_fd: bool = False):
    """Try to acquire an exclusive lock on lock_file.

    Returns True if acquired (or fd if return_fd=True), False/None if locked.
    Uses fcntl.flock(LOCK_EX | LOCK_NB) for non-blocking acquire.

    When return_fd=True the caller MUST close the returned fd to release
    the lock.  When return_fd=False the fd is closed immediately (which
    releases the lock) — this is a check-only mode. Note: in check-only
    mode, if flock succeeds but fd.close() raises, the lock is released
    (fd closed by GC) but the exception propagates — this is intentional
    since a failed close indicates a deeper I/O problem.

    On platforms without fcntl (Windows) locking is unavailable, so this
    degrades to a permissive check (returns True / an fd that does nothing)
    rather than crashing the scheduler — cross-process cron dedup then
    relies on the caller's own guards.
    """
    if fcntl is None:
        # No cross-process lock available; pretend acquired so single-
        # process scheduling still works.
        if return_fd:
            fd = open(lock_file, "w")
            return fd
        return True
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd = open(lock_file, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if return_fd:
            return fd
        # check-only: close fd to release the lock immediately
        fd.close()
        return True
    except OSError:
        if fd is not None:
            fd.close()
        if return_fd:
            return None
        return False


def _release_lock(fd) -> None:
    """Release the file lock and close the fd.

    Releases the lock first, then closes the fd. If closing fails,
    the fd is still unlocked but may leak — we log a warning.
    """
    if fcntl is not None:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
    try:
        fd.close()
    except Exception:
        logger.warning("Failed to close cron lock file descriptor")


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------


_SAFE_JOB_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _validate_schedule(schedule: str) -> None:
    """Fail fast on malformed schedules (raises ValueError).

    - cron: must parse via CronTrigger.from_crontab
    - interval:N: N must be a positive integer (APScheduler silently
      clamps 0→1s and accepts negative intervals — both are nonsense
      for a cron job)
    """
    if schedule.startswith("interval:"):
        raw = schedule.split(":", 1)[1]
        try:
            seconds = int(raw)
        except ValueError:
            raise ValueError(f"invalid schedule {schedule!r}: interval must be an integer") from None
        if seconds <= 0:
            raise ValueError(f"invalid schedule {schedule!r}: interval must be > 0 seconds")
        return
    from apscheduler.triggers.cron import CronTrigger

    try:
        CronTrigger.from_crontab(schedule)
    except (ValueError, KeyError) as e:
        raise ValueError(f"invalid schedule {schedule!r}: {e}") from None


def _sanitize_job_name(name: str) -> str:
    """Reduce a job name to a safe single path component.

    Job names flow into the output directory path unsanitized — a name
    like '../../escaped' would otherwise write cron output anywhere on
    disk. Slashes and other specials become '_'; leading dots/underscores
    are stripped so the result can never be '.'/'..' or a hidden dir.
    """
    safe = _SAFE_JOB_NAME.sub("_", name).strip("._")
    return safe or "unnamed"


def _save_cron_output(base_dir: Path, job_id: str, prompt: str, response: str) -> str:
    """Save cron job output to disk.

    Returns the file path. Format: {base_dir}/{job_id}/{timestamp}.md
    """
    out_dir = base_dir / "output" / _sanitize_job_name(job_id)
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
        import asyncio

        # Serializes job executions: each job temporarily sets the shared
        # runner's session_id to its own cron-scoped id, and overlapping
        # ticks must not interleave that swap.
        self._exec_lock = asyncio.Lock()
        if not self.lock_path:
            self.lock_path = str(Path.home() / ".microagent" / "cron.lock")

    @staticmethod
    def _session_id_for(job: CronJob) -> str:
        """Per-job session id — jobs no longer share one session (which made
        resume:last pick up other jobs' or the user's interactive history)."""
        return f"cron-{_sanitize_job_name(job.name)}"

    def add_job(self, job: CronJob) -> None:
        """Add a job. If scheduler is running, schedule immediately."""
        # Defense in depth: the name becomes a directory under
        # ~/.microagent/cron/output/ — reject path separators outright
        # (output writing also sanitizes, but failing fast is friendlier).
        if "/" in job.name or "\\" in job.name or job.name in (".", "..", ""):
            raise ValueError(f"unsafe cron job name: {job.name!r}")
        # Validate the schedule BEFORE registering — previously a malformed
        # expression on a running scheduler raised from _schedule_job AFTER
        # the job had landed in self.jobs (registered but never scheduled).
        _validate_schedule(job.schedule)
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

            async with self._exec_lock:
                if job.session_strategy == "resume:last" and self.store is not None:
                    messages = await self._build_resume_messages(job)
                else:
                    messages = [Message.user(job.prompt)]

                # Run under the job's own session id so its history neither
                # pollutes nor reads other sessions. The shared runner's
                # original id is restored afterwards. Memory is skipped
                # (Hermes invariant: cron sessions pass skip_memory=True) —
                # scheduled background ticks must not write memories of
                # themselves or inject unrelated memory context.
                runner = getattr(self.agent, "runner", None)
                if runner is not None:
                    prev_sid = runner.session_id
                    prev_skip = getattr(runner, "skip_memory", False)
                    runner.session_id = self._session_id_for(job)
                    runner.skip_memory = True
                    try:
                        result = await self.agent.arun(messages)
                    finally:
                        runner.session_id = prev_sid
                        runner.skip_memory = prev_skip
                else:
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
        """Build messages for resume:last strategy — load THIS JOB's last
        session history and append the prompt.

        Previously this picked the most recent session in the whole store,
        so a resume:last job could continue another job's conversation —
        or the user's interactive session — injecting the scheduled prompt
        into completely unrelated context.
        """
        from ..core.types import Message

        try:
            history = await self.store.load_history(self._session_id_for(job))
            if not history:
                return [Message.user(job.prompt)]

            # Append job prompt as a new user message to the continued conversation
            return list(history) + [Message.user(job.prompt)]
        except Exception:
            # Store failure — fall back to fresh conversation
            return [Message.user(job.prompt)]
