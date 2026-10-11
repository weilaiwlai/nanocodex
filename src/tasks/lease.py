from __future__ import annotations

import threading
from collections.abc import Callable

from src.tasks.task_graph import renew_task_lease
from src.tasks.task_store import TaskConflict


class LeaseHeartbeat:
    def __init__(self, *, tasks_dir, task: dict, owner_agent_id: str,
                 lease_seconds: float = 30, on_lost: Callable[[], None] | None = None):
        self.tasks_dir, self.task, self.owner_agent_id = tasks_dir, task, owner_agent_id
        self.lease_seconds, self.on_lost = lease_seconds, on_lost
        self.stopped = threading.Event()
        self.lost = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stopped.wait(max(0.02, self.lease_seconds / 3)):
            try:
                task = renew_task_lease(tasks_dir=self.tasks_dir, task_id=self.task["id"],
                                        owner_agent_id=self.owner_agent_id,
                                        lease_token=self.task["lease_token"], lease_seconds=self.lease_seconds)
                if task["status"] != "running":
                    if task['status'] == 'completed':
                        return
                    raise TaskConflict(f"task_{task['id']} 已停止执行。")
            except Exception as exc:
                self.error = exc
                self.lost.set()
                if self.on_lost:
                    self.on_lost()
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.thread.join(timeout=11)
