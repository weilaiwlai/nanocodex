"""JSONL control channel shared by the CLI and TUI."""
from __future__ import annotations

import json
import threading


def apply_control(context, packet: dict) -> bool:
    action = packet.get('type')
    if action == 'cancel':
        context.cancel_run()
        return True
    if action == 'permission_response':
        # Never interpret strings such as 'false' as approval.
        approved = packet.get('approved')
        if not isinstance(approved, bool):
            return False
        return context.permission_broker.resolve(str(packet.get('request_id', '')), approved)
    return False


def start_control_reader(context, stream):
    def read():
        while True:
            line = stream.readline(65537)
            if not line:
                context.cancel_run()
                return
            if len(line) > 65536:
                continue
            try:
                packet = json.loads(line)
                if isinstance(packet, dict):
                    apply_control(context, packet)
            except (ValueError, TypeError):
                continue
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread
