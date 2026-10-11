"""Small blocking calls without tying asyncio shutdown to an executor thread."""
from __future__ import annotations

import asyncio
import threading


async def async_call(function, /, *args, **kwargs):
    done = threading.Event()
    outcome = {}
    def run():
        try:
            outcome['value'] = function(*args, **kwargs)
        except BaseException as exc:
            outcome['error'] = exc
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    while not done.is_set():
        await asyncio.sleep(0.02)
    if 'error' in outcome:
        raise outcome['error']
    return outcome.get('value')
