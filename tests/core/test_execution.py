import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.runtime.execution import CommandManager
from src.runtime.permissions import ExecutionPolicy, PermissionBroker
from src.runtime.streaming import controlled_events, controlled_run, RunCancelled
from src.runtime.session import ToolRuntimeContext


class TestProcessPolicy:
    """Test-only adapter to exercise process lifecycle when kernel namespaces are blocked."""
    def command(self, command, cwd, scratch):
        return ['/bin/bash', '--noprofile', '--norc', '-c', command]
    def environment(self, scratch):
        return {'PATH': os.environ['PATH'], 'HOME': str(scratch)}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = CommandManager()
        self.addCleanup(self.manager.close)

    def start(self, command, **kwargs):
        return self.manager.start(command, cwd=self.root, policy=TestProcessPolicy(), output_dir=self.root / 'logs', **kwargs)

    def test_output_visible_before_completion(self):
        session = self.start('printf early; sleep 0.4; printf late')
        deadline = time.monotonic() + 1
        while 'early' not in self.manager.poll(session.id)['stdout'] and time.monotonic() < deadline:
            time.sleep(0.01)
        result = self.manager.poll(session.id)
        self.assertIn('early', result['stdout'])
        self.assertEqual('running', result['status'])
        self.assertEqual('earlylate', self.manager.wait(session.id)['stdout'])

    def test_timeout_and_partial_output(self):
        session = self.start('printf before; sleep 5', timeout=0.1)
        result = self.manager.wait(session.id)
        self.assertEqual('timed_out', result['status'])
        self.assertEqual('before', result['stdout'])
        self.assertLess(result['elapsed_ms'], 1500)

    def test_cancel_process_tree(self):
        session = self.start('sleep 30 & printf "%s" "$!"; wait')
        deadline = time.monotonic() + 1
        while not self.manager.poll(session.id)['stdout'] and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(self.manager.poll(session.id)['stdout'])
        self.manager.cancel(session.id)
        result = self.manager.wait(session.id)
        self.assertEqual('cancelled', result['status'])
        stat = Path(f'/proc/{child_pid}/stat')
        if stat.exists():
            self.assertIn(stat.read_text().split()[2], {'Z', 'X'})

    def test_cancel_event(self):
        session = self.start('sleep 30')
        stop = threading.Event()
        stop.set()
        self.assertEqual('cancelled', self.manager.wait(session.id, cancelled=stop)['status'])

    def test_output_bounds_and_log(self):
        session = self.start("python3 -c 'print(\"x\" * 100000)' ")
        result = self.manager.wait(session.id)
        self.assertLessEqual(len(result['stdout']), 65536)
        self.assertTrue(result['output_truncated'])
        self.assertGreater(Path(result['log_path']).stat().st_size, 90000)

    def test_exit_failure_and_cursor(self):
        session = self.start('printf out; printf error >&2; exit 7')
        result = self.manager.wait(session.id)
        self.assertEqual('failed', result['status'])
        self.assertEqual(7, result['exit_code'])
        self.assertEqual([], self.manager.poll(session.id, after=result['cursor'])['events'])

    def test_owner_isolation(self):
        a = self.start('sleep 3', owner='a')
        b = self.start('sleep 3', owner='b')
        self.manager.cancel_owner('a')
        self.assertEqual('cancelled', self.manager.wait(a.id)['status'])
        self.assertEqual('running', self.manager.poll(b.id)['status'])

    def test_os_sandbox_integration(self):
        binary = shutil.which('bwrap')
        if not binary:
            self.skipTest('bubblewrap missing')
        probe = subprocess.run([binary, '--unshare-all', '--ro-bind', '/', '/', '--', '/bin/true'], capture_output=True, timeout=5)
        if probe.returncode:
            self.skipTest('kernel blocks bubblewrap namespace setup')
        (self.root / '.env').write_text('private-value')
        session = self.manager.start('cat .env; touch result', cwd=self.root,
                                     policy=ExecutionPolicy(self.root, PermissionBroker()), output_dir=self.root / 'logs')
        result = self.manager.wait(session.id)
        self.assertNotIn('private-value', result['stdout'])
        self.assertTrue((self.root / 'result').exists())


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.context = ToolRuntimeContext(session_root=root, session_dir=root, workspace_root=root, execution_root=root)
        self.addCleanup(self.context.command_manager.close)

    async def test_runtime_events_delivered_while_sdk_waits(self):
        class SDKResult:
            async def stream_events(self):
                await asyncio.sleep(0.1)
                yield 'model-event'
            def cancel(self): pass
        self.context.emit_runtime_event('command_output', {'text': 'live'})
        events = [event async for event in controlled_events(SDKResult(), self.context)]
        self.assertEqual('runtime', events[0][0])
        self.assertEqual(('sdk', 'model-event'), events[1])

    async def test_stream_cancel_is_prompt(self):
        cancelled = []
        class SDKResult:
            async def stream_events(self):
                await asyncio.sleep(30)
                yield 'unreachable'
            def cancel(self): cancelled.append(True)
        self.context.cancel_event.set()
        with self.assertRaises(RunCancelled):
            async for _ in controlled_events(SDKResult(), self.context): pass
        self.assertEqual([True], cancelled)

    async def test_teammate_cancel(self):
        self.context.cancel_event.set()
        with self.assertRaises(RunCancelled):
            await controlled_run(asyncio.sleep(30), self.context)
