from __future__ import annotations

import asyncio
from src.runtime.async_utils import async_call
from agents import RunContextWrapper, function_tool
from src.protocol import success_response
from src.runtime.session import ToolRuntimeContext
from src.tools.common import ToolFailure, error_from_failure, build_context, build_stats, start_timer, run_traced_tool_async


async def _request_permissions(ctx: RunContextWrapper[ToolRuntimeContext], reason: str,
                               read_paths: list[str] | None = None, write_paths: list[str] | None = None,
                               network: bool = False):
    start = start_timer()
    args = dict(reason=reason, read_paths=read_paths or [], write_paths=write_paths or [], network=network)
    def request():
        root = ctx.context.execution_root
        normalize = lambda paths: [str((root / path).resolve()) for path in paths]
        return ctx.context.permission_broker.request(
            reason=reason, read_paths=normalize(read_paths or []), write_paths=normalize(write_paths or []),
            network=network, cancelled=ctx.context.cancel_event)
    async def invoke():
        approved = await async_call(request)
        return success_response(data={'approved': approved}, text='权限已批准。' if approved else '权限未批准，当前权限保持不变。',
                                stats=build_stats(start), context=build_context(params_input=args))
    return await run_traced_tool_async(ctx.context, tool_name='RequestPermissions', params_input=args, invoke=invoke)


def command_control(context, process_id, *, cancel=False, after=0):
    start = start_timer()
    args = dict(process_id=process_id, after=after)
    try:
        session = context.command_manager.get(process_id)
        if context.actor_name != 'team-lead' and session.owner != context.actor_name:
            raise ValueError('只能操作自己启动的命令。')
        result = context.command_manager.cancel(process_id) if cancel else context.command_manager.poll(process_id, after=after)
        return success_response(data=result, text=f'{process_id}: {result["status"]}\n{result["stdout"]}\n{result["stderr"]}',
                                stats=build_stats(start), context=build_context(params_input=args))
    except (KeyError, ValueError) as exc:
        return error_from_failure(ToolFailure(code='INVALID_PARAM', message=str(exc), text=str(exc)),
                                  start_time=start, params_input=args)


def _command_poll(ctx: RunContextWrapper[ToolRuntimeContext], process_id: str, after: int = 0):
    return command_control(ctx.context, process_id, after=max(0, after))


async def _command_cancel(ctx: RunContextWrapper[ToolRuntimeContext], process_id: str):
    return await async_call(command_control, ctx.context, process_id, cancel=True)


EXECUTION_TOOLS = [
    function_tool(_request_permissions, name_override='RequestPermissions', description_override='申请额外路径或网络权限；只有用户批准后生效。'),
    function_tool(_command_poll, name_override='CommandPoll', description_override='查询前台或后台命令的状态、输出和增量游标。'),
    function_tool(_command_cancel, name_override='CommandCancel', description_override='取消命令并终止其进程树。'),
]
