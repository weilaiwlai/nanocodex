from __future__ import annotations

import hashlib
import json
import re

from agents import RunContextWrapper, function_tool

from src.protocol import ToolResponse, success_response
from src.runtime.session import ToolRuntimeContext
from src.tasks.task_store import TaskConflict
from src.tasks.workflow import (
    activate_workflow,
    define_workflow,
    fail_workflow_start,
    get_workflow_definition,
    get_workflow_run,
    list_workflow_definitions,
    start_workflow,
)
from src.tasks.worktrees import closeout_workflow_run, ensure_workflow_run_worktree
from src.tools.common import (
    ToolFailure,
    build_context,
    build_stats,
    error_from_failure,
    run_traced_tool,
    start_timer,
)

MAX_WORKFLOW_JSON_CHARS = 128_000
MAX_WORKERS = 8


def _require_context(context: ToolRuntimeContext | None) -> ToolRuntimeContext:
    if context is None or context.session is None:
        raise ToolFailure(code="NO_SESSION", message="当前没有可用 session。", text="当前没有可用会话，无法操作工作流。")
    if context.team_runtime is None:
        raise ToolFailure(code="NO_TEAM_RUNTIME", message="当前 session 没有 team runtime。", text="当前会话尚未初始化 Agent Team。")
    return context


def _ensure_workflow_teammates(context: ToolRuntimeContext, definition: dict) -> list[str]:
    runtime = context.team_runtime
    members = runtime.list_teammates()["members"]
    active_statuses = {"spawning", "working", "idle"}
    routes = {(step.get("role"), step.get("agent")) for step in definition["steps"]}
    if len(routes) > MAX_WORKERS:
        raise ToolFailure(
            code="INVALID_PARAM",
            message=f"工作流最多可以启动 {MAX_WORKERS} 个不同 Agent 路由。",
            text=f"工作流定义需要 {len(routes)} 个不同 Agent 路由，当前上限是 {MAX_WORKERS}。",
        )
    active_members = [member for member in members if member.get("status") in active_statuses]
    if len(active_members) > MAX_WORKERS:
        raise ToolFailure(
            code="INVALID_PARAM",
            message=f"Agent Team 超过安全上限 {MAX_WORKERS}。",
            text=f"当前活跃 Agent 数量为 {len(active_members)}，超过上限 {MAX_WORKERS}。",
        )
    required_spawns = set()
    planned_agent_names = {member.get("name") for member in active_members}
    planned_roles = {member.get("role") for member in active_members}
    for step in definition["steps"]:
        role, agent_name = step.get("role"), step.get("agent")
        if agent_name:
            if agent_name not in planned_agent_names:
                required_spawns.add((role, agent_name))
                planned_agent_names.add(agent_name)
                if role:
                    planned_roles.add(role)
        elif role not in planned_roles:
            required_spawns.add((role, None))
            planned_roles.add(role)
    if len(active_members) + len(required_spawns) > MAX_WORKERS:
        raise ToolFailure(
            code="INVALID_PARAM",
            message=f"工作流启动后 Agent 数量会超过 {MAX_WORKERS}。",
            text=f"当前有 {len(active_members)} 个活跃 Agent，工作流还需要启动 {len(required_spawns)} 个。",
        )
    spawned: list[str] = []
    handled: set[tuple[str | None, str | None]] = set()

    for step in definition["steps"]:
        role = step.get("role")
        agent_name = step.get("agent")
        key = (role, agent_name)
        if key in handled:
            continue
        handled.add(key)

        if agent_name:
            existing = next((m for m in members if m.get("name") == agent_name), None)
            if existing and existing.get("status") in active_statuses:
                if role and existing.get("role") != role:
                    raise ToolFailure(
                        code="INVALID_PARAM",
                        message=f"teammate '{agent_name}' 的角色与工作流步骤不匹配。",
                        text=f"teammate '{agent_name}' 当前角色为 {existing.get('role')}，工作流要求 {role}。",
                    )
                continue
            resolved_role = role or (existing.get("role") if existing else None) or "general"
            runtime.spawn_teammate(
                name=agent_name,
                role=resolved_role,
                prompt=f"你是工作流 {definition['name']} 的 {resolved_role}。现在调用 ClaimTask 获取分配给你的步骤；若暂时没有任务就报告等待。完成任务时使用传入的上游结果并给出简短结果摘要。",
                persistent=True,
            )
            spawned.append(agent_name)
            members = runtime.list_teammates()["members"]
            continue

        existing = next((m for m in members if m.get("role") == role and m.get("status") in active_statuses), None)
        if existing:
            continue

        safe_role = re.sub(r"[^A-Za-z0-9_-]+", "-", str(role)).strip("-").lower()[:32] or "worker"
        suffix = hashlib.sha1(str(role).encode("utf-8")).hexdigest()[:6]
        name = f"wf-{safe_role}-{suffix}"
        runtime.spawn_teammate(
            name=name,
            role=str(role),
            prompt=f"你是工作流 {definition['name']} 的 {role}。现在调用 ClaimTask 获取分配给你的步骤；若暂时没有任务就报告等待。完成任务时使用传入的上游结果并给出简短结果摘要。",
            persistent=True,
        )
        spawned.append(name)
        members = runtime.list_teammates()["members"]

    return spawned


def _workflow_define_tool(ctx: RunContextWrapper[ToolRuntimeContext], workflow_json: str) -> ToolResponse:
    start = start_timer()
    params = {"workflow_json": workflow_json[:MAX_WORKFLOW_JSON_CHARS]}
    try:
        context = _require_context(ctx.context)
        if len(workflow_json) > MAX_WORKFLOW_JSON_CHARS:
            raise ValueError(f"工作流定义不能超过 {MAX_WORKFLOW_JSON_CHARS} 个字符。")
        payload = json.loads(workflow_json)
        if not isinstance(payload, dict) or set(payload) != {"name", "steps"}:
            raise ValueError("workflow_json 必须只包含 name 和 steps 字段。")
        definition = define_workflow(
            tasks_dir=context.tasks_dir,
            name=payload["name"],
            steps=payload["steps"],
        )
        return success_response(
            data={"workflow": definition},
            text=f"已保存工作流 '{definition['name']}'，workflow_id={definition['id']}。",
            stats=build_stats(start),
            context=build_context(params_input={"name": definition["name"]}, session_id=context.session_id),
        )
    except (ValueError, TaskConflict) as exc:
        return error_from_failure(
            ToolFailure(code="INVALID_PARAM", message=str(exc), text=str(exc)),
            start_time=start,
            params_input=params,
            session_id=ctx.context.session_id,
        )
    except ToolFailure as failure:
        return error_from_failure(failure, start_time=start, params_input=params, session_id=ctx.context.session_id)


def _workflow_start_tool(ctx: RunContextWrapper[ToolRuntimeContext], workflow_id: str) -> ToolResponse:
    start = start_timer()
    params = {"workflow_id": workflow_id}
    context = ctx.context
    try:
        context = _require_context(context)
        definition = get_workflow_definition(tasks_dir=context.tasks_dir, workflow_id=workflow_id)
        run_result = start_workflow(tasks_dir=context.tasks_dir, workflow_id=workflow_id, activate=False)
        run_id = run_result["run"]["id"]
        try:
            if run_result["run"].get("require_worktree"):
                ensure_workflow_run_worktree(runtime_context=context, run_id=run_id)
            spawned = _ensure_workflow_teammates(context, definition)
            activate_workflow(tasks_dir=context.tasks_dir, run_id=run_id)
        except Exception as exc:
            fail_workflow_start(tasks_dir=context.tasks_dir, run_id=run_id, error=str(exc))
            try:
                closeout_workflow_run(runtime_context=context, run_id=run_id, action="remove")
            except Exception:
                pass
            raise
        run_result = get_workflow_run(tasks_dir=context.tasks_dir, run_id=run_id)
        run = run_result["run"]
        return success_response(
            data={
                "run": run,
                "spawned_teammates": spawned,
                "tasks": [{"id": task["id"], "step_id": task["workflow_step_id"], "status": task["status"]}
                          for task in run_result["tasks"]],
            },
            text=f"已启动工作流 '{definition['name']}'，run_id={run['id']}。",
            stats=build_stats(start),
            context=build_context(params_input=params, session_id=context.session_id),
        )
    except FileNotFoundError as exc:
        return error_from_failure(
            ToolFailure(code="NOT_FOUND", message=str(exc), text=str(exc)),
            start_time=start,
            params_input=params,
            session_id=ctx.context.session_id,
        )
    except (ValueError, TaskConflict) as exc:
        return error_from_failure(
            ToolFailure(code="INVALID_PARAM", message=str(exc), text=str(exc)),
            start_time=start,
            params_input=params,
            session_id=ctx.context.session_id,
        )
    except ToolFailure as failure:
        return error_from_failure(failure, start_time=start, params_input=params, session_id=ctx.context.session_id)
    except Exception as exc:
        return error_from_failure(
            ToolFailure(code="EXECUTION_ERROR", message=str(exc), text=f"工作流启动失败：{exc}"),
            start_time=start,
            params_input=params,
            session_id=ctx.context.session_id,
        )


def _workflow_status_tool(ctx: RunContextWrapper[ToolRuntimeContext], run_id: str) -> ToolResponse:
    start = start_timer()
    params = {"run_id": run_id}
    try:
        context = _require_context(ctx.context)
        result = get_workflow_run(tasks_dir=context.tasks_dir, run_id=run_id)
        return success_response(
            data=result,
            text=f"工作流运行 {run_id} 当前状态：{result['run']['status']}。",
            stats=build_stats(start),
            context=build_context(params_input=params, session_id=context.session_id),
        )
    except FileNotFoundError as exc:
        return error_from_failure(
            ToolFailure(code="NOT_FOUND", message=str(exc), text=str(exc)),
            start_time=start,
            params_input=params,
            session_id=ctx.context.session_id,
        )
    except ToolFailure as failure:
        return error_from_failure(failure, start_time=start, params_input=params, session_id=ctx.context.session_id)


def _workflow_list_tool(ctx: RunContextWrapper[ToolRuntimeContext]) -> ToolResponse:
    start = start_timer()
    try:
        context = _require_context(ctx.context)
        definitions = list_workflow_definitions(tasks_dir=context.tasks_dir)
        return success_response(
            data={"workflows": definitions},
            text=f"当前共有 {len(definitions)} 个工作流定义。",
            stats=build_stats(start, total=len(definitions)),
            context=build_context(params_input={}, session_id=context.session_id),
        )
    except ToolFailure as failure:
        return error_from_failure(failure, start_time=start, params_input={}, session_id=ctx.context.session_id)


def _workflow_closeout_tool(
    ctx: RunContextWrapper[ToolRuntimeContext],
    run_id: str,
    action: str,
) -> ToolResponse:
    start = start_timer()
    params = {"run_id": run_id, "action": action}
    try:
        context = _require_context(ctx.context)
        run = closeout_workflow_run(runtime_context=context, run_id=run_id, action=action)
        return success_response(
            data={"run": run},
            text=f"已对工作流 {run_id} 执行 closeout：{action}。",
            stats=build_stats(start),
            context=build_context(params_input=params, session_id=context.session_id),
        )
    except FileNotFoundError as exc:
        return error_from_failure(
            ToolFailure(code="NOT_FOUND", message=str(exc), text=str(exc)),
            start_time=start,
            params_input=params,
            session_id=ctx.context.session_id,
        )
    except ToolFailure as failure:
        return error_from_failure(failure, start_time=start, params_input=params, session_id=ctx.context.session_id)


def _workflow_define_handler(ctx: RunContextWrapper[ToolRuntimeContext], workflow_json: str) -> ToolResponse:
    return run_traced_tool(
        ctx.context,
        tool_name="WorkflowDefine",
        params_input={"workflow_json": workflow_json[:MAX_WORKFLOW_JSON_CHARS]},
        invoke=lambda: _workflow_define_tool(ctx, workflow_json),
    )


def _workflow_start_handler(ctx: RunContextWrapper[ToolRuntimeContext], workflow_id: str) -> ToolResponse:
    return run_traced_tool(
        ctx.context,
        tool_name="WorkflowStart",
        params_input={"workflow_id": workflow_id},
        invoke=lambda: _workflow_start_tool(ctx, workflow_id),
    )


def _workflow_status_handler(ctx: RunContextWrapper[ToolRuntimeContext], run_id: str) -> ToolResponse:
    return run_traced_tool(
        ctx.context,
        tool_name="WorkflowStatus",
        params_input={"run_id": run_id},
        invoke=lambda: _workflow_status_tool(ctx, run_id),
    )


def _workflow_list_handler(ctx: RunContextWrapper[ToolRuntimeContext]) -> ToolResponse:
    return run_traced_tool(
        ctx.context,
        tool_name="WorkflowList",
        params_input={},
        invoke=lambda: _workflow_list_tool(ctx),
    )


def _workflow_closeout_handler(
    ctx: RunContextWrapper[ToolRuntimeContext],
    run_id: str,
    action: str,
) -> ToolResponse:
    return run_traced_tool(
        ctx.context,
        tool_name="WorkflowCloseout",
        params_input={"run_id": run_id, "action": action},
        invoke=lambda: _workflow_closeout_tool(ctx, run_id, action),
    )


workflow_define_tool = function_tool(
    _workflow_define_handler,
    name_override="WorkflowDefine",
    description_override="保存可复用的多 Agent 工作流定义。workflow_json 使用 {name,steps}，每步含 step_id、title、prompt、role 或 agent、depends_on，可选 input_from、require_worktree、max_retries。",
)
workflow_start_tool = function_tool(
    _workflow_start_handler,
    name_override="WorkflowStart",
    description_override="启动已保存的工作流；自动创建缺少的角色 Agent，按依赖解锁步骤并把上游结果交给后续 Agent。",
)
workflow_status_tool = function_tool(
    _workflow_status_handler,
    name_override="WorkflowStatus",
    description_override="查看工作流运行状态和各步骤结果。",
)
workflow_list_tool = function_tool(
    _workflow_list_handler,
    name_override="WorkflowList",
    description_override="列出当前 session 保存的工作流定义。",
)
workflow_closeout_tool = function_tool(
    _workflow_closeout_handler,
    name_override="WorkflowCloseout",
    description_override="对工作流共享 worktree 执行 keep、remove 或 merge。merge 仅接受已完成且 worktree、主工作区均干净的运行。",
)

WORKFLOW_TOOLS = [workflow_define_tool, workflow_start_tool, workflow_status_tool, workflow_list_tool, workflow_closeout_tool]
