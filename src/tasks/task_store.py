from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


class TaskConflict(RuntimeError):
    """The caller's task version or execution lease is stale."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def ensure_tasks_dir(tasks_dir: Path) -> None:
    tasks_dir.mkdir(parents=True, exist_ok=True)


def get_task_path(tasks_dir: Path, task_id: int) -> Path:
    """Legacy JSON path, retained for migration compatibility."""
    return tasks_dir / f"task_{task_id}.json"


class TaskTransaction:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, task_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT payload, revision FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(f"task_{task_id} 不存在。")
        return dict(json.loads(row[0]), revision=row[1])

    def list(self) -> list[dict[str, Any]]:
        ids = self.connection.execute("SELECT id FROM tasks ORDER BY id").fetchall()
        return [self.get(row[0]) for row in ids]

    def put(self, task: dict[str, Any]) -> dict[str, Any]:
        current = self.get(int(task["id"]))
        if task.get("revision") != current["revision"]:
            raise TaskConflict(f"task_{task['id']} 已被另一个执行者更新。")
        updated = dict(task, revision=current["revision"] + 1, updated_at=_utc_now())
        self.connection.execute("UPDATE tasks SET payload=?, revision=? WHERE id=?",
                                (json.dumps(updated, ensure_ascii=False), updated["revision"], updated["id"]))
        return updated

    def insert(self, task: dict[str, Any]) -> dict[str, Any]:
        cursor = self.connection.execute("INSERT INTO tasks(payload,revision) VALUES ('{}',0)")
        created = dict(task, id=cursor.lastrowid, revision=0)
        self.connection.execute("UPDATE tasks SET payload=? WHERE id=?",
                                (json.dumps(created, ensure_ascii=False), created["id"]))
        return created


@contextmanager
def task_transaction(tasks_dir: Path) -> Iterator[TaskTransaction]:
    ensure_tasks_dir(tasks_dir)
    connection = sqlite3.connect(tasks_dir / "tasks.sqlite3", timeout=10, isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)")
        connection.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)")
        if connection.execute("SELECT 1 FROM migrations WHERE name='legacy_json'").fetchone() is None:
            for path in sorted(tasks_dir.glob("task_*.json")):
                task = json.loads(path.read_text(encoding="utf-8"))
                connection.execute("INSERT OR IGNORE INTO tasks(id,payload,revision) VALUES (?,?,0)",
                                   (int(task["id"]), json.dumps(task, ensure_ascii=False)))
            connection.execute("INSERT INTO migrations(name) VALUES ('legacy_json')")
        yield TaskTransaction(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_next_task_id(tasks_dir: Path) -> int:
    with task_transaction(tasks_dir) as store:
        return max((task["id"] for task in store.list()), default=0) + 1


def save_task(tasks_dir: Path, task: dict[str, Any]) -> dict[str, Any]:
    with task_transaction(tasks_dir) as store:
        return store.put(task)


def get_task(tasks_dir: Path, task_id: int) -> dict[str, Any]:
    with task_transaction(tasks_dir) as store:
        return store.get(task_id)


def list_tasks(tasks_dir: Path) -> list[dict[str, Any]]:
    with task_transaction(tasks_dir) as store:
        return store.list()


def create_task(*, tasks_dir: Path, title: str, summary: str, kind: str,
                prompt: str | None = None, subagent_type: str | None = None,
                model_route: str | None = None, require_worktree: bool = False,
                owner: str = "main_agent", status: str = "pending") -> dict[str, Any]:
    now = _utc_now()
    task = dict(title=title.strip(), summary=summary.strip(), kind=kind, prompt=prompt or "",
                status=status, owner=owner, owner_agent_id=None, subagent_type=subagent_type,
                model_route=model_route, require_worktree=require_worktree, blockedBy=[], blocks=[],
                lease_expires_at=None, lease_token=None, lease_generation=0, worktree_name=None,
                worktree_path=None, result_summary=None, result_artifact=None, error=None,
                created_at=now, updated_at=now)
    with task_transaction(tasks_dir) as store:
        return store.insert(task)
