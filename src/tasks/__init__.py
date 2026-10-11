"""Task storage remains usable without loading the model SDK."""

from importlib import import_module

_EXPORTS = {
    "create_task": "task_store", "get_task": "task_store", "list_tasks": "task_store",
    "update_task": "task_graph", "run_subagent_task": "subagent",
    "start_background_command": "background", "drain_notifications": "background",
    "mark_interrupted_running_tasks": "background",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f"src.tasks.{_EXPORTS[name]}"), name)
    globals()[name] = value
    return value
