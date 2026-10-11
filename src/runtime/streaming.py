from __future__ import annotations

import asyncio


class RunCancelled(RuntimeError):
    pass


async def controlled_events(result, context):
    """Pump command/approval events even while the SDK awaits a model or tool."""
    iterator = result.stream_events().__aiter__()
    pending = asyncio.create_task(anext(iterator))
    try:
        while True:
            if context is not None:
                for name, payload in context.drain_runtime_events():
                    yield ('runtime', (name, payload))
                if context.cancel_event.is_set():
                    result.cancel()
                    context.cancel_run()
                    raise RunCancelled('用户取消了当前运行。')
            done, _ = await asyncio.wait({pending}, timeout=0.05)
            if not done:
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            yield ('sdk', event)
            pending = asyncio.create_task(anext(iterator))
        if context is not None:
            for name, payload in context.drain_runtime_events():
                yield ('runtime', (name, payload))
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def controlled_run(awaitable, context):
    """Cancellable non-streaming run for teammates and analysis tasks."""
    pending = asyncio.create_task(awaitable)
    try:
        while not pending.done():
            if context.cancel_event.is_set():
                raise RunCancelled('运行已取消或任务租约已丢失。')
            await asyncio.wait({pending}, timeout=0.05)
        return pending.result()
    finally:
        if not pending.done():
            pending.cancel()
            context.command_manager.cancel_owner(context.actor_name)
        await asyncio.gather(pending, return_exceptions=True)
