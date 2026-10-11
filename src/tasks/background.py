from __future__ import annotations

import threading
from typing import Any

from src.runtime.session import ToolRuntimeContext
from src.tasks.task_graph import update_task
from src.tasks.task_store import create_task, list_tasks


def start_background_command(*, runtime_context: ToolRuntimeContext, command: str,
                             title: str | None = None, timeout_ms: int = 120_000) -> dict[str, Any]:
    # Use exactly the same validation, policy and process manager as foreground Bash.
    from src.tools.bash_tool import _validate_command, _validate_timeout
    command = _validate_command(command)
    timeout_ms = _validate_timeout(timeout_ms)
    task = create_task(tasks_dir=runtime_context.tasks_dir, title=(title or command).strip()[:60],
                       summary=command, kind="background_command", prompt=command,
                       owner="background", status="running")
    try:
        session = runtime_context.command_manager.start(
            command, cwd=runtime_context.execution_root, policy=runtime_context.execution_policy(),
            output_dir=runtime_context.session_dir / "commands", timeout=timeout_ms / 1000,
            owner=runtime_context.actor_name, emit=runtime_context.emit_runtime_event)
    except Exception as exc:
        update_task(tasks_dir=runtime_context.tasks_dir, task_id=task["id"], status="failed", error=str(exc))
        raise
    task = update_task(tasks_dir=runtime_context.tasks_dir, task_id=task["id"], result_artifact=str(session.log_path))
    def finish():
        try:
            result = runtime_context.command_manager.wait(session.id, cancelled=runtime_context.cancel_event)
            status = "completed" if result["status"] == "completed" else "failed"
            summary = f'{command}: {result["status"]} (exit {result["exit_code"]})'
            updated = update_task(tasks_dir=runtime_context.tasks_dir, task_id=task["id"], status=status,
                                  result_summary=summary, error=None if status == "completed" else summary)
            runtime_context.enqueue_background_notification(
                task_id=task["id"], text=f'task_{task["id"]} {updated["status"]}: {summary}')
        except Exception as exc:
            runtime_context.emit_runtime_event("background_error", {"task_id": task["id"], "message": str(exc)})
    threading.Thread(target=finish, daemon=True).start()
    return dict(task_id=task["id"], status=task["status"], process_id=session.id, log_path=str(session.log_path))


def drain_notifications(runtime_context: ToolRuntimeContext) -> list[dict[str, object]]:
    return runtime_context.drain_background_notifications()


def mark_interrupted_running_tasks(*, tasks_dir) -> int:
    updated_count = 0
    for task in list_tasks(tasks_dir):
        if task.get("status") != "running" or task.get("kind") != "background_command":
            continue
        update_task(tasks_dir=tasks_dir, task_id=task["id"], status="failed",
                    error="process exited before background command completed")
        updated_count += 1
    return updated_count
