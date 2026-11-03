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
        available_at = _parse_utc(task.get("available_at"))
        return available_at is None or available_at <= now
    expires = _parse_utc(task.get("lease_expires_at"))
    return task["status"] == "running" and expires is not None and expires <= now


def claim_task(*, tasks_dir, owner_agent_id: str, owner: str, lease_seconds: float,
               task_id: int | None = None, agent_name: str | None = None,
               agent_role: str | None = None) -> dict[str, Any] | None:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds 必须大于零。")
    with task_transaction(tasks_dir) as store:
        now = _utc_now()
        for task in ([store.get(task_id)] if task_id is not None else store.list()):
            workflow_run_id = task.get("workflow_run_id")
            if workflow_run_id:
                try:
                    workflow_run = store.get_workflow_run(str(workflow_run_id))
                except FileNotFoundError:
                    continue
                # WorkflowStart prepares workers and any shared worktree before
                # exposing tasks to the polling workers.
                if workflow_run.get("status") != "running":
                    continue
                if workflow_run.get("require_worktree"):
                    # A shared worktree is a single mutable checkout. Keep its
                    # steps serial even when the DAG has multiple ready nodes.
                    another_live_step = False
                    for other_id in workflow_run.get("task_ids", {}).values():
                        if int(other_id) == int(task["id"]):
                            continue
                        other = store.get(int(other_id))
                        expires = _parse_utc(other.get("lease_expires_at"))
                        if other.get("status") == "running" and expires is not None and expires > now:
                            another_live_step = True
                            break
                    if another_live_step:
                        continue
            if task.get("assigned_agent") and task["assigned_agent"] != agent_name:
                continue
            if task.get("assigned_role") and task["assigned_role"] != agent_role:
                continue
            if _task_is_claimable(task, now=now):
                task.update(status="running", owner=owner, owner_agent_id=owner_agent_id,
                            lease_expires_at=_format_utc(now + timedelta(seconds=lease_seconds)),
                            lease_token=uuid4().hex, lease_generation=task.get("lease_generation", 0) + 1,
                            attempts=int(task.get("attempts", 0)) + 1, available_at=None)
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
            task["depends_on"] = _merge_ids(task.get("depends_on", []), add_blocked_by)
        if add_blocks:
            if task_id in add_blocks:
                raise ValueError("任务不能依赖自身。")
            task["blocks"] = _merge_ids(task.get("blocks", []), add_blocks)
            for other_id in add_blocks:
                other = store.get(other_id)
                other["blockedBy"] = _merge_ids(other.get("blockedBy", []), [task_id])
                other["depends_on"] = _merge_ids(other.get("depends_on", []), [task_id])
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
        workflow_run_id = task.get("workflow_run_id")
        if workflow_run_id:
            _refresh_workflow_run(store, str(workflow_run_id))
        return task


def _refresh_workflow_run(store, run_id: str) -> dict[str, Any]:
    try:
        run = store.get_workflow_run(run_id)
    except FileNotFoundError:
        return {}
    task_ids = run.get("task_ids", {}).values()
    statuses = [store.get(int(task_id))["status"] for task_id in task_ids]
    if statuses and all(status == "completed" for status in statuses):
        run["status"] = "completed"
        run["finished_at"] = _format_utc(_utc_now())
    elif any(status in {"failed", "cancelled"} for status in statuses):
        run["status"] = "failed"
        run["finished_at"] = _format_utc(_utc_now())
    else:
        run["status"] = "running"
    return store.put_workflow_run(run)


def fail_task_attempt(*, tasks_dir, task_id: int, error: str,
                      expected_owner_agent_id: str, expected_lease_token: str) -> dict[str, Any]:
    """Retry a failed attempt within its configured bound, otherwise fail the task."""
    with task_transaction(tasks_dir) as store:
        task = store.get(task_id)
        _check_owner(task, expected_owner_agent_id, expected_lease_token)
        expires = _parse_utc(task.get("lease_expires_at"))
        if task["status"] != "running" or expires is None or expires <= _utc_now():
            raise TaskConflict(f"task_{task_id} 不再持有有效执行租约。")
        task["error"] = error
        attempts = int(task.get("attempts", 1))
        max_retries = max(0, int(task.get("max_retries", 0)))
        if attempts <= max_retries:
            delay = max(0.0, float(task.get("retry_delay_seconds", 0)))
            task["status"] = "pending"
            task["available_at"] = _format_utc(_utc_now() + timedelta(seconds=delay))
        else:
            task["status"] = "failed"
            task["available_at"] = None
        task.update(owner_agent_id=None, lease_expires_at=None, lease_token=None)
        task = store.put(task)
        if task.get("status") == "failed" and task.get("workflow_run_id"):
            _refresh_workflow_run(store, str(task["workflow_run_id"]))
        return task
