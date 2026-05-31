from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from src.runtime.session import ToolRuntimeContext
from src.tasks.task_store import get_task, list_tasks, save_task
from src.tools.common import ToolFailure


def _worktrees_dir(runtime_context: ToolRuntimeContext) -> Path:
    return runtime_context.session_dir / "worktrees"


def _task_branch_name(task_id: int) -> str:
    return f"task/{task_id}"


def _task_worktree_path(runtime_context: ToolRuntimeContext, task_id: int) -> Path:
    return _worktrees_dir(runtime_context) / f"task_{task_id}"


def _run_git_command(*, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ToolFailure(
            code="EXECUTION_ERROR",
            message=completed.stderr.strip() or "git 命令执行失败。",
            text="执行 git 命令时失败，请检查当前仓库状态。",
        )
    return completed


def ensure_task_worktree(
    *,
    runtime_context: ToolRuntimeContext,
    task_id: int,
) -> dict[str, Any]:
    # 一个任务绑定一个专属 worktree 和分支；如果已存在，就直接复用。
    task = get_task(runtime_context.tasks_dir, task_id)
    worktree_path = _task_worktree_path(runtime_context, task_id)
    branch_name = _task_branch_name(task_id)

    if task.get("worktree_path"):
        return task

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    # 为每个 task 创建独立分支，方便后续 merge 回主分支。
    _run_git_command(
        args=["git", "worktree", "add", "-b", branch_name, str(worktree_path), "HEAD"],
        cwd=runtime_context.workspace_root,
    )
    resolved_worktree_path = worktree_path.resolve()
    task["worktree_name"] = f"task-{task_id}"
    task["worktree_path"] = str(resolved_worktree_path)
    task["worktree_branch"] = branch_name
    return save_task(runtime_context.tasks_dir, task)


def list_worktrees(*, runtime_context: ToolRuntimeContext) -> list[dict[str, Any]]:
    worktrees: list[dict[str, Any]] = []
    for task in list_tasks(runtime_context.tasks_dir):
        worktree_path = task.get("worktree_path")
        if not worktree_path:
            continue
        entry: dict[str, Any] = {
            "task_id": task["id"],
            "task_title": task["title"],
            "worktree_name": task.get("worktree_name"),
            "worktree_path": worktree_path,
            "worktree_branch": task.get("worktree_branch"),
            "exists": Path(worktree_path).exists(),
        }
        # 检查 worktree 目录是否有未提交的修改。
        if entry["exists"]:
            try:
                status = _run_git_command(
                    args=["git", "status", "--porcelain"],
                    cwd=Path(worktree_path),
                )
                entry["dirty"] = bool(status.stdout.strip())
            except ToolFailure:
                entry["dirty"] = None
        else:
            entry["dirty"] = None
        worktrees.append(entry)
    return worktrees


def closeout_task_worktree(
    *,
    runtime_context: ToolRuntimeContext,
    task_id: int,
    action: str,
) -> dict[str, Any]:
    # closeout 支持 keep / remove / merge 三种决策。
    if action not in {"keep", "remove", "merge"}:
        raise ToolFailure(
            code="INVALID_PARAM",
            message=f"非法 closeout 动作: {action}",
            text="参数错误：closeout 只支持 keep、remove 或 merge。",
        )

    task = get_task(runtime_context.tasks_dir, task_id)
    worktree_path = str(task.get("worktree_path") or "").strip()
    if not worktree_path:
        raise ToolFailure(
            code="NOT_FOUND",
            message=f"task_{task_id} 当前没有绑定 worktree。",
            text=f"task_{task_id} 当前没有可 closeout 的 worktree。",
        )

    if action == "merge":
        branch_name = str(task.get("worktree_branch") or "").strip()
        if not branch_name:
            raise ToolFailure(
                code="NOT_FOUND",
                message=f"task_{task_id} 没有关联分支，无法 merge。",
                text=f"task_{task_id} 的 worktree 没有关联分支，无法执行 merge。",
            )
        # 先把 task 分支合并回主分支，再清理 worktree。
        _run_git_command(
            args=["git", "merge", "--no-ff", branch_name, "-m", f"merge task/{task_id}: {task.get('title', '')}"],
            cwd=runtime_context.workspace_root,
        )
        # merge 成功后，清理 worktree 和分支。
        if Path(worktree_path).exists():
            _run_git_command(
                args=["git", "worktree", "remove", "--force", worktree_path],
                cwd=runtime_context.workspace_root,
            )
        try:
            _run_git_command(
                args=["git", "branch", "-d", branch_name],
                cwd=runtime_context.workspace_root,
            )
        except ToolFailure:
            # 分支删除失败不影响主流程。
            pass
        task["worktree_name"] = None
        task["worktree_path"] = None
        task["worktree_branch"] = None
        updated = save_task(runtime_context.tasks_dir, task)
        if runtime_context.team_runtime is not None:
            runtime_context.team_runtime.clear_worktree_binding(worktree_path=worktree_path)
        return updated

    if action == "remove":
        branch_name = str(task.get("worktree_branch") or "").strip()
        if Path(worktree_path).exists():
            _run_git_command(
                args=["git", "worktree", "remove", "--force", worktree_path],
                cwd=runtime_context.workspace_root,
            )
        # 清理关联分支（忽略失败）。
        if branch_name:
            try:
                _run_git_command(
                    args=["git", "branch", "-D", branch_name],
                    cwd=runtime_context.workspace_root,
                )
            except ToolFailure:
                pass
        task["worktree_name"] = None
        task["worktree_path"] = None
        task["worktree_branch"] = None
        updated = save_task(runtime_context.tasks_dir, task)
        if runtime_context.team_runtime is not None:
            runtime_context.team_runtime.clear_worktree_binding(worktree_path=worktree_path)
        return updated

    return task
