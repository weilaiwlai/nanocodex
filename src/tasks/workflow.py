from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from src.tasks.task_store import TaskConflict, get_task, task_transaction

MAX_WORKFLOW_STEPS = 32
MAX_RETRIES = 5


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def normalize_definition(*, name: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(name, str):
        raise ValueError("工作流名称必须是字符串。")
    name = name.strip()
    if not name or len(name) > 120:
        raise ValueError("工作流名称不能为空且不能超过 120 个字符。")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_WORKFLOW_STEPS:
        raise ValueError(f"工作流必须包含 1 到 {MAX_WORKFLOW_STEPS} 个步骤。")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed = {
        "step_id", "title", "summary", "prompt", "role", "agent",
        "depends_on", "input_from", "require_worktree", "max_retries",
        "retry_delay_seconds",
    }
    for raw in steps:
        if not isinstance(raw, dict):
            raise ValueError("每个工作流步骤必须是 JSON 对象。")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"步骤包含未知字段：{', '.join(sorted(unknown))}")
        step_id = str(raw.get("step_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", step_id):
            raise ValueError("step_id 必须以字母或数字开头，且只包含字母、数字、下划线或短横线。")
        if step_id in seen:
            raise ValueError(f"step_id 重复：{step_id}")
        seen.add(step_id)

        title = str(raw.get("title") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        role = str(raw.get("role") or "").strip()
        agent = str(raw.get("agent") or "").strip() or None
        if not title or not prompt:
            raise ValueError(f"步骤 {step_id} 必须提供 title 和 prompt。")
        if not role and not agent:
            raise ValueError(f"步骤 {step_id} 必须指定 role 或 agent。")
        if len(role) > 80 or (agent and len(agent) > 80):
            raise ValueError(f"步骤 {step_id} 的 role 或 agent 名称过长。")

        depends_on = raw.get("depends_on", [])
        input_from = raw.get("input_from", depends_on)
        if not isinstance(depends_on, list) or not all(isinstance(x, str) for x in depends_on):
            raise ValueError(f"步骤 {step_id} 的 depends_on 必须是字符串数组。")
        if not isinstance(input_from, list) or not all(isinstance(x, str) for x in input_from):
            raise ValueError(f"步骤 {step_id} 的 input_from 必须是字符串数组。")
        if len(set(depends_on)) != len(depends_on) or len(set(input_from)) != len(input_from):
            raise ValueError(f"步骤 {step_id} 的依赖不能重复。")
        if set(input_from) - set(depends_on):
            raise ValueError(f"步骤 {step_id} 的 input_from 必须来自 depends_on。")

        max_retries = raw.get("max_retries", 0)
        retry_delay = raw.get("retry_delay_seconds", 1)
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= MAX_RETRIES:
            raise ValueError(f"步骤 {step_id} 的 max_retries 必须在 0 到 {MAX_RETRIES} 之间。")
        if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)) or not 0 <= retry_delay <= 300:
            raise ValueError(f"步骤 {step_id} 的 retry_delay_seconds 必须在 0 到 300 之间。")
        require_worktree = raw.get("require_worktree", False)
        if not isinstance(require_worktree, bool):
            raise ValueError(f"步骤 {step_id} 的 require_worktree 必须是布尔值。")

        normalized.append({
            "step_id": step_id,
            "title": title[:160],
            "summary": str(raw.get("summary") or title).strip()[:2000],
            "prompt": prompt,
            "role": role or None,
            "agent": agent,
            "depends_on": list(depends_on),
            "input_from": list(input_from),
            "require_worktree": require_worktree,
            "max_retries": max_retries,
            "retry_delay_seconds": float(retry_delay),
        })

    ids = {step["step_id"] for step in normalized}
    named_agent_roles: dict[str, str] = {}
    for step in normalized:
        missing = set(step["depends_on"]) - ids
        if missing:
            raise ValueError(f"步骤 {step['step_id']} 引用了不存在的依赖：{', '.join(sorted(missing))}")
        agent = step.get("agent")
        role = step.get("role")
        if agent and role:
            previous_role = named_agent_roles.setdefault(agent, role)
            if previous_role != role:
                raise ValueError(f"固定 Agent {agent} 不能在同一工作流中承担多个角色。")
    for step in normalized:
        if step.get("agent") and not step.get("role") and step["agent"] in named_agent_roles:
            step["role"] = named_agent_roles[step["agent"]]

    graph = {step["step_id"]: step["depends_on"] for step in normalized}
    visited: set[str] = set()
    active: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in active:
            raise ValueError("工作流步骤依赖形成了循环。")
        if step_id in visited:
            return
        active.add(step_id)
        for dependency in graph[step_id]:
            visit(dependency)
        active.remove(step_id)
        visited.add(step_id)

    for step_id in graph:
        visit(step_id)

    now = _utc_now()
    return {
        "id": f"workflow-{uuid4().hex}",
        "name": name,
        "steps": normalized,
        "created_at": now,
        "updated_at": now,
    }


def define_workflow(*, tasks_dir, name: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    definition = normalize_definition(name=name, steps=steps)
    with task_transaction(tasks_dir) as store:
        return store.insert_workflow_definition(definition)


def get_workflow_definition(*, tasks_dir, workflow_id: str) -> dict[str, Any]:
    with task_transaction(tasks_dir) as store:
        return store.get_workflow_definition(workflow_id)


def list_workflow_definitions(*, tasks_dir) -> list[dict[str, Any]]:
    with task_transaction(tasks_dir) as store:
        return store.list_workflow_definitions()


def start_workflow(*, tasks_dir, workflow_id: str, activate: bool = True) -> dict[str, Any]:
    now = _utc_now()
    run_id = f"run-{uuid4().hex}"
    with task_transaction(tasks_dir) as store:
        definition = store.get_workflow_definition(workflow_id)
        share_worktree = any(step["require_worktree"] for step in definition["steps"])
        tasks_by_step: dict[str, dict[str, Any]] = {}
        for step in definition["steps"]:
            tasks_by_step[step["step_id"]] = store.insert({
                "title": step["title"],
                "summary": step["summary"],
                "kind": "workflow_step",
                "prompt": step["prompt"],
                "status": "pending",
                "owner": "workflow",
                "owner_agent_id": None,
                "subagent_type": None,
                "model_route": None,
                "require_worktree": share_worktree,
                "assigned_agent": step["agent"],
                "assigned_role": step["role"],
                "workflow_definition_id": workflow_id,
                "workflow_run_id": run_id,
                "workflow_step_id": step["step_id"],
                "depends_on": [],
                "input_from": [],
                "blockedBy": [],
                "blocks": [],
                "attempts": 0,
                "max_retries": step["max_retries"],
                "retry_delay_seconds": step["retry_delay_seconds"],
                "available_at": None,
                "lease_expires_at": None,
                "lease_token": None,
                "lease_generation": 0,
                "worktree_name": None,
                "worktree_path": None,
                "result_summary": None,
                "result_artifact": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
            })

        task_ids = {step_id: int(task["id"]) for step_id, task in tasks_by_step.items()}
        for step in definition["steps"]:
            task = store.get(task_ids[step["step_id"]])
            dependency_ids = [task_ids[item] for item in step["depends_on"]]
            input_ids = [task_ids[item] for item in step["input_from"]]
            task["depends_on"] = dependency_ids
            task["input_from"] = input_ids
            task["blockedBy"] = list(dependency_ids)
            store.put(task)
            for dependency_id in dependency_ids:
                upstream = store.get(dependency_id)
                upstream["blocks"] = sorted(set([*upstream.get("blocks", []), task["id"]]))
                store.put(upstream)

        run = {
            "id": run_id,
            "definition_id": workflow_id,
            "definition_name": definition["name"],
            "status": "running" if activate else "preparing",
            "task_ids": task_ids,
            "require_worktree": share_worktree,
            "worktree_name": None,
            "worktree_path": None,
            "worktree_branch": None,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        store.insert_workflow_run(run)
        return {"run": run, "tasks": [store.get(task_id) for task_id in task_ids.values()]}


def activate_workflow(*, tasks_dir, run_id: str) -> dict[str, Any]:
    with task_transaction(tasks_dir) as store:
        run = store.get_workflow_run(run_id)
        if run.get("status") != "preparing":
            raise ValueError(f"工作流运行 {run_id} 当前不能启动。")
        run["status"] = "running"
        return store.put_workflow_run(run)


def get_workflow_run(*, tasks_dir, run_id: str) -> dict[str, Any]:
    with task_transaction(tasks_dir) as store:
        run = store.get_workflow_run(run_id)
        return {
            "run": run,
            "tasks": [store.get(int(task_id)) for task_id in run.get("task_ids", {}).values()],
        }


def fail_workflow_start(*, tasks_dir, run_id: str, error: str) -> None:
    with task_transaction(tasks_dir) as store:
        run = store.get_workflow_run(run_id)
        for task_id in run.get("task_ids", {}).values():
            task = store.get(int(task_id))
            if task["status"] in {"pending", "blocked", "running"}:
                task.update(status="cancelled", error=error, owner_agent_id=None,
                            lease_expires_at=None, lease_token=None)
                store.put(task)
        run.update(status="failed", error=error, finished_at=_utc_now())
        store.put_workflow_run(run)


def dependency_results(*, tasks_dir, task: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for task_id in task.get("input_from", []):
        upstream = get_task(tasks_dir, int(task_id))
        results.append({
            "task_id": upstream["id"],
            "step_id": upstream.get("workflow_step_id"),
            "status": upstream["status"],
            "result_summary": upstream.get("result_summary"),
            "result_artifact": upstream.get("result_artifact"),
        })
    return results
