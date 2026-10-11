from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from src.tasks.task_store import TaskConflict, task_transaction

VALID_TASK_STATUSES = {"pending", "running", "blocked", "completed", "cancelled", "failed"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _merge_ids(existing: list[int], new_ids: list[int]) -> list[int]:
    return sorted({int(item) for item in [*existing, *new_ids]})


def _task_is_claimable(task: dict[str, Any], *, now: datetime) -> bool:
    if task.get("blockedBy") or task.get("kind") == "background_command":
        return False
    if task["status"] == "pending":
        return True
    expires = _parse_utc(task.get("lease_expires_at"))
    return task["status"] == "running" and expires is not None and expires <= now


def claim_task(*, tasks_dir, owner_agent_id: str, owner: str, lease_seconds: float, task_id: int | None = None) -> dict[str, Any] | None:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds 必须大于零。")
    with task_transaction(tasks_dir) as store:
        now = _utc_now()
        for task in ([store.get(task_id)] if task_id is not None else store.list()):
            if _task_is_claimable(task, now=now):
                task.update(status="running", owner=owner, owner_agent_id=owner_agent_id,
                            lease_expires_at=_format_utc(now + timedelta(seconds=lease_seconds)),
                            lease_token=uuid4().hex, lease_generation=task.get("lease_generation", 0) + 1)
                return store.put(task)
    return None


def _check_owner(task, owner_agent_id=None, lease_token=None) -> None:
    if owner_agent_id is not None and task.get("owner_agent_id") != owner_agent_id:
        raise TaskConflict(f"task_{task['id']} 已不属于当前执行者。")
    if lease_token is not None and task.get("lease_token") != lease_token:
        raise TaskConflict(f"task_{task['id']} 的租约已失效。")


def renew_task_lease(*, tasks_dir, task_id: int, owner_agent_id: str,
                     lease_seconds: float, lease_token: str | None = None) -> dict[str, Any]:
    with task_transaction(tasks_dir) as store:
        task = store.get(task_id)
        _check_owner(task, owner_agent_id, lease_token)
        if task["status"] != "running":
            return task
        now = _utc_now()
        expires = _parse_utc(task.get("lease_expires_at"))
        if expires is None or expires <= now:
            raise TaskConflict(f"task_{task_id} 的租约已过期。")
        task["lease_expires_at"] = _format_utc(now + timedelta(seconds=lease_seconds))
        return store.put(task)


def update_task(*, tasks_dir, task_id: int, status: str | None = None,
                add_blocked_by: list[int] | None = None, add_blocks: list[int] | None = None,
                owner: str | None = None, result_summary: str | None = None,
                result_artifact: str | None = None, error: str | None = None,
                expected_owner_agent_id: str | None = None,
                expected_lease_token: str | None = None) -> dict[str, Any]:
    with task_transaction(tasks_dir) as store:
        task = store.get(task_id)
        _check_owner(task, expected_owner_agent_id, expected_lease_token)
        if expected_lease_token is not None:
            expires = _parse_utc(task.get('lease_expires_at'))
            if task['status'] != 'running' or expires is None or expires <= _utc_now():
                raise TaskConflict(f'task_{task_id} 不再持有有效执行租约。')
        if status is not None:
            if status not in VALID_TASK_STATUSES:
                raise ValueError(f"非法任务状态: {status}")
            task["status"] = status
        for key, value in dict(owner=owner, result_summary=result_summary,
                               result_artifact=result_artifact, error=error).items():
            if value is not None:
                task[key] = value
        if status in {"running", "completed"} and error is None:
            task["error"] = None
        if add_blocked_by:
            if task_id in add_blocked_by:
                raise ValueError("任务不能依赖自身。")
            unresolved = []
            for other_id in add_blocked_by:
                other = store.get(other_id)
                if other['status'] != 'completed':
                    unresolved.append(other_id)
                    other['blocks'] = _merge_ids(other.get('blocks', []), [task_id])
                    store.put(other)
            task["blockedBy"] = _merge_ids(task.get("blockedBy", []), unresolved)
        if add_blocks:
            if task_id in add_blocks:
                raise ValueError("任务不能依赖自身。")
            task["blocks"] = _merge_ids(task.get("blocks", []), add_blocks)
            for other_id in add_blocks:
                other = store.get(other_id)
                other["blockedBy"] = _merge_ids(other.get("blockedBy", []), [task_id])
                store.put(other)
        task = store.put(task)
        if status == "completed":
            for other in store.list():
                if task_id in other.get("blockedBy", []):
                    other["blockedBy"].remove(task_id)
                    store.put(other)
        # Validate the entire graph before committing any dependency changes.
        graph = {item['id']: item.get('blockedBy', []) for item in store.list()}
        visited, active = set(), set()
        def visit(node):
            if node in active:
                raise ValueError('任务依赖形成了循环。')
            if node in visited:
                return
            active.add(node)
            for parent in graph[node]:
                visit(parent)
            active.remove(node)
            visited.add(node)
        for node in graph:
            visit(node)
        return task
