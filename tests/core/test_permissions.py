import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime.permissions import ExecutionPolicy, PermissionBroker, PermissionDenied, SandboxUnavailable
from src.runtime.control import apply_control


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.broker = PermissionBroker(timeout=0.1)
        self.policy = ExecutionPolicy(self.root, self.broker)

    def test_scope_secrets_and_git(self):
        self.policy.authorize_path(self.root / 'code.py', write=True)
        for path, write in [(self.root.parent / 'other', False), (self.root / '.env.production', False),
                            (self.root / '.git' / 'config', True), (self.root / '.ssh' / 'id_rsa', False)]:
            with self.subTest(path=path), self.assertRaises(PermissionDenied):
                self.policy.authorize_path(path, write=write)
        self.policy.authorize_path(self.root / '.env.example')

    def test_symlink_escape(self):
        (self.root / 'link').symlink_to(self.root.parent)
        with self.assertRaises(PermissionDenied):
            self.policy.authorize_path(self.root / 'link' / 'other')

    def test_explicit_grants(self):
        self.broker.grant(read_paths=[self.root / '.env.production'], write_paths=[self.root / '.git'])
        self.policy.authorize_path(self.root / '.env.production')
        self.policy.authorize_path(self.root / '.git' / 'config', write=True)

    def test_environment_does_not_leak_keys(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'AWS_SECRET_ACCESS_KEY': 'secret', 'PATH': '/bin'}):
            env = self.policy.environment(self.root)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
        self.assertEqual('/bin', env['PATH'])

    def test_approval_denial_timeout_and_cancellation(self):
        self.broker.handler = lambda packet: False
        self.assertFalse(self.broker.request(network=True))
        self.assertFalse(self.broker.network)
        self.broker.handler = lambda packet: time.sleep(1)
        started = time.monotonic()
        self.assertFalse(self.broker.request(network=True))
        self.assertLess(time.monotonic() - started, 0.5)
        self.broker = PermissionBroker(timeout=0.1)
        self.broker.handler = lambda packet: True
        self.assertTrue(self.broker.request(network=True))
        self.assertTrue(self.broker.network)
        stop = threading.Event()
        stop.set()
        self.assertFalse(self.broker.request(read_paths=[self.root.parent], cancelled=stop))

    def test_missing_sandbox_fails_closed(self):
        with patch('src.runtime.permissions.shutil.which', return_value=None):
            with self.assertRaises(SandboxUnavailable):
                self.policy.command('true', self.root, self.root)

    def test_bwrap_mounts_and_network_default(self):
        nested = self.root / 'nested'
        nested.mkdir()
        (nested / '.env.local').write_text('secret')
        (self.root / '.git').mkdir()
        (self.root / 'scratch').mkdir()
        with patch('src.runtime.permissions.shutil.which', return_value='/usr/bin/bwrap'), patch('src.runtime.permissions.sys.platform', 'linux'):
            args = self.policy.command('true', self.root, self.root / 'scratch')
        self.assertIn('--unshare-all', args)
        self.assertNotIn('--share-net', args)
        self.assertIn(str(nested / '.env.local'), args)
        self.assertIn(str(self.root / '.git'), args)

    def test_control_does_not_approve_string_false(self):
        class Context:
            permission_broker = self.broker
        self.assertFalse(apply_control(Context(), {'type': 'permission_response', 'request_id': 'x', 'approved': 'false'}))
