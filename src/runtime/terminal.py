from __future__ import annotations

import select
import sys
import time


def terminal_permission(payload):
    if not sys.stdin.isatty():
        return False
    paths = ', '.join([*payload['read_paths'], *payload['write_paths']])
    print(f"\n权限申请: {payload['reason']}\n路径: {paths or '(无)'} | 联网: {payload['network']}\n批准? [y/N] ",
          end='', file=sys.stderr, flush=True)
    if sys.platform == 'win32':
        import msvcrt
        deadline = time.monotonic() + payload['timeout_seconds']
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return msvcrt.getwch().lower() == 'y'
            time.sleep(0.05)
        return False
    readable, _, _ = select.select([sys.stdin], [], [], payload['timeout_seconds'])
    return bool(readable) and sys.stdin.readline().strip().lower() in {'y', 'yes'}
