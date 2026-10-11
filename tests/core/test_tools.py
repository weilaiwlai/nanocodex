"""Exercise tool business logic offline; the SDK decorator is stubbed only when absent."""
import importlib.util
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if 'agents' not in sys.modules and importlib.util.find_spec('agents') is None:
    stub = types.ModuleType('agents')
    class Wrapper:
        def __class_getitem__(cls, item): return cls
    stub.RunContextWrapper = Wrapper
    stub.function_tool = lambda function, **options: function
    sys.modules['agents'] = stub

from src.runtime.session import ToolRuntimeContext
from src.tools.read_only import read_file, grep_search as grep, list_files
from src.tools.edit_write import write_file
from src.tools.bash_tool import run_bash
from src.tasks.background import start_background_command, mark_interrupted_running_tasks
from src.tasks.task_store import create_task, get_task
from tests.core.test_execution import TestProcessPolicy


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        workspace = self.root / 'repo'
        workspace.mkdir()
        self.context = ToolRuntimeContext(workspace_root=workspace, execution_root=workspace,
                                          session_root=self.root, session_dir=self.root / 'session',
                                          tasks_dir=self.root / 'tasks')
        self.addCleanup(self.context.command_manager.close)

    def test_read_write_permissions_and_symlinks(self):
        (self.context.execution_root / '.env').write_text('SECRET')
        (self.context.execution_root / 'outside').symlink_to(self.root)
        self.assertEqual('error', read_file('.env', runtime_context=self.context)['status'])
        self.assertEqual('error', read_file('outside/missing', runtime_context=self.context)['status'])
        self.assertEqual('error', write_file('.git/config', 'bad', runtime_context=self.context)['status'])

    def test_grep_filters_secret_and_symlink(self):
        (self.context.execution_root / 'safe.py').write_text('needle')
        (self.root / 'outside.txt').write_text('needle private')
        (self.context.execution_root / 'linked.txt').symlink_to(self.root / 'outside.txt')
        (self.context.execution_root / 'private.pem').write_text('needle secret')
        with patch('src.tools.read_only.shutil.which', return_value=None):
            result = grep('needle', runtime_context=self.context)
        self.assertEqual(['safe.py'], [match['file'] for match in result['data']['matches']])

    def test_bash_and_background_share_policy_and_lifecycle(self):
        seen = []
        manager = self.context.command_manager
        original = manager.start
        def start(command, **kwargs):
            seen.append(type(kwargs['policy']).__name__)
            kwargs['policy'] = TestProcessPolicy()
            return original(command, **kwargs)
        with patch.object(manager, 'start', side_effect=start):
            foreground = run_bash('printf foreground', runtime_context=self.context)
            background = start_background_command(runtime_context=self.context, command='printf background')
            manager.wait(background['process_id'])
            deadline = time.monotonic() + 2
            while get_task(self.context.tasks_dir, background['task_id'])['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual(['ExecutionPolicy', 'ExecutionPolicy'], seen)
        self.assertIn('foreground', foreground['data']['stdout'])
        self.assertEqual('completed', get_task(self.context.tasks_dir, background['task_id'])['status'])

    def test_recovery_only_marks_background(self):
        normal = create_task(tasks_dir=self.context.tasks_dir, title='normal', summary='', kind='code', status='running')
        bg = create_task(tasks_dir=self.context.tasks_dir, title='bg', summary='', kind='background_command', status='running')
        self.assertEqual(1, mark_interrupted_running_tasks(tasks_dir=self.context.tasks_dir))
        self.assertEqual('running', get_task(self.context.tasks_dir, normal['id'])['status'])
        self.assertEqual('failed', get_task(self.context.tasks_dir, bg['id'])['status'])
