"""
Scheduled tasks — run recurring tasks via a simple cron-like scheduler.

scheduler that can run periodic tasks like:
  - Sending daily summaries
  - Running health checks
  - Periodic memory consolidation
  - Scheduled messages

Uses asyncio for non-blocking scheduling (no external cron daemon needed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

TASKS_FILE = os.getenv("SCHEDULED_TASKS_FILE", "")
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "false").lower() in ("1", "true", "yes")


@dataclass
class ScheduledTask:
    id: str
    name: str
    interval_seconds: int
    callback_name: str  # registered callback name
    platform: str = ""
    user_id: str = ""
    enabled: bool = True
    last_run: float = 0.0
    run_count: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def next_run(self) -> float:
        return self.last_run + self.interval_seconds

    @property
    def is_due(self) -> bool:
        return time.time() >= self.next_run

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "callback_name": self.callback_name,
            "platform": self.platform,
            "user_id": self.user_id,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "run_count": self.run_count,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ScheduledTask:
        return cls(
            id=d["id"], name=d["name"],
            interval_seconds=d["interval_seconds"],
            callback_name=d["callback_name"],
            platform=d.get("platform", ""),
            user_id=d.get("user_id", ""),
            enabled=d.get("enabled", True),
            last_run=d.get("last_run", 0.0),
            run_count=d.get("run_count", 0),
            extra=d.get("extra", {}),
        )


# Callback type: receives the task and returns True on success
TaskCallback = Callable[[ScheduledTask], Awaitable[bool]]


class Scheduler:
    """Simple in-process task scheduler using asyncio."""

    def __init__(self, tasks_file: str = ""):
        self._tasks: dict[str, ScheduledTask] = {}
        self._callbacks: dict[str, TaskCallback] = {}
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._tasks_file = tasks_file or TASKS_FILE

    def register_callback(self, name: str, callback: TaskCallback) -> None:
        self._callbacks[name] = callback

    def add_task(self, task: ScheduledTask) -> None:
        self._tasks[task.id] = task
        self._save()

    def remove_task(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._save()
            return True
        return False

    def get_task(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    def list_user_tasks(self, platform: str, user_id: str) -> list[ScheduledTask]:
        return [t for t in self._tasks.values()
                if t.platform == platform and t.user_id == user_id]

    def start(self) -> None:
        if self._running:
            return
        self._load()
        self._running = True
        self._loop_task = asyncio.create_task(self._run_loop())
        log.info("Scheduler started with %d tasks", len(self._tasks))

    def stop(self) -> None:
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        self._save()

    async def _run_loop(self) -> None:
        while self._running:
            try:
                now = time.time()
                for task in list(self._tasks.values()):
                    if not task.enabled or not task.is_due:
                        continue
                    callback = self._callbacks.get(task.callback_name)
                    if not callback:
                        log.warning("No callback registered for task %s (%s)",
                                    task.id, task.callback_name)
                        continue
                    try:
                        success = await callback(task)
                        task.last_run = now
                        task.run_count += 1
                        if not success:
                            log.warning("Task %s returned failure", task.id)
                    except Exception:
                        log.exception("Task %s failed", task.id)
                        task.last_run = now
                        task.run_count += 1
                self._save()
            except Exception:
                log.exception("Scheduler loop error")
            await asyncio.sleep(10)  # check every 10 seconds

    def _save(self) -> None:
        if not self._tasks_file:
            return
        try:
            data = [t.to_dict() for t in self._tasks.values()]
            Path(self._tasks_file).write_text(json.dumps(data, indent=2))
        except Exception:
            log.debug("Failed to save tasks", exc_info=True)

    def _load(self) -> None:
        if not self._tasks_file:
            return
        try:
            data = json.loads(Path(self._tasks_file).read_text())
            for d in data:
                task = ScheduledTask.from_dict(d)
                self._tasks[task.id] = task
        except (OSError, json.JSONDecodeError):
            pass


# Global scheduler instance
scheduler = Scheduler()
