import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.tasks.task_store import create_task, get_task, list_tasks, save_task, TaskConflict
from src.tasks.task_graph import claim_task, update_task
from src.tasks.lease import LeaseHeartbeat


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def create(self, **kwargs):
        return create_task(tasks_dir=self.root, title='task', summary='goal', kind='code', **kwargs)

    def claim(self, owner='a', seconds=30, task_id=None):
        return claim_task(tasks_dir=self.root, owner_agent_id=owner, owner=owner, lease_seconds=seconds, task_id=task_id)

    def test_concurrent_create_and_claim(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            created = list(pool.map(lambda _: self.create(), range(36)))
            claimed = list(pool.map(lambda n: self.claim(str(n)), range(48)))
        self.assertEqual(36, len({t['id'] for t in created}))
        self.assertEqual(36, len([t for t in claimed if t]))
        self.assertEqual(36, len({t['id'] for t in claimed if t}))

    def test_migrate_legacy_once_and_reopen(self):
        task = dict(id=7, title='old', summary='', status='pending', kind='code', blockedBy=[], blocks=[])
        (self.root / 'task_7.json').write_text(json.dumps(task))
        self.assertEqual('old', get_task(self.root, 7)['title'])
        update_task(tasks_dir=self.root, task_id=7, result_summary='new')
        self.assertEqual('new', get_task(self.root, 7)['result_summary'])
        self.assertEqual(8, self.create()['id'])

    def test_stale_revision_rejected(self):
        task = self.create()
        update_task(tasks_dir=self.root, task_id=task['id'], result_summary='new')
        with self.assertRaises(TaskConflict):
            save_task(self.root, task)

    def test_expired_worker_cannot_commit(self):
        self.create()
        old = self.claim(seconds=0.05)
        time.sleep(0.08)
        with self.assertRaises(TaskConflict):
            update_task(tasks_dir=self.root, task_id=old['id'], status='completed', expected_lease_token=old['lease_token'])
        new = self.claim('b')
        self.assertNotEqual(old['lease_token'], new['lease_token'])
        with self.assertRaises(TaskConflict):
            update_task(tasks_dir=self.root, task_id=old['id'], status='completed', expected_owner_agent_id='a', expected_lease_token=old['lease_token'])

    def test_heartbeat_prevents_takeover(self):
        self.create()
        task = self.claim(seconds=0.3)
        with LeaseHeartbeat(tasks_dir=self.root, task=task, owner_agent_id='a', lease_seconds=0.3) as heartbeat:
            time.sleep(0.65)
            self.assertIsNone(self.claim('b'))
            self.assertFalse(heartbeat.lost.is_set())

    def test_heartbeat_loss_cancels_worker(self):
        self.create()
        task = self.claim(seconds=0.3)
        cancelled = []
        with LeaseHeartbeat(tasks_dir=self.root, task=task, owner_agent_id='a', lease_seconds=0.3,
                            on_lost=lambda: cancelled.append(True)) as heartbeat:
            current = get_task(self.root, task['id'])
            current['lease_token'] = 'replacement'
            save_task(self.root, current)
            self.assertTrue(heartbeat.lost.wait(2))
            self.assertEqual([True], cancelled)

    def test_dependencies_and_cycle_rollback(self):
        a, b = self.create(), self.create()
        update_task(tasks_dir=self.root, task_id=b['id'], add_blocked_by=[a['id']])
        with self.assertRaises(ValueError):
            update_task(tasks_dir=self.root, task_id=a['id'], add_blocked_by=[b['id']])
        self.assertEqual([], get_task(self.root, a['id'])['blockedBy'])
        self.assertEqual(a['id'], self.claim()['id'])
        self.assertIsNone(self.claim('b'))
        update_task(tasks_dir=self.root, task_id=a['id'], status='completed')
        self.assertEqual(b['id'], self.claim('b')['id'])

    def test_background_not_claimed(self):
        self.create(status='running')
        create_task(tasks_dir=self.root, title='bg', summary='', kind='background_command')
        self.assertIsNone(self.claim())

    def test_cancelled_task_stops_heartbeat_worker(self):
        self.create()
        task = self.claim(seconds=0.3)
        with LeaseHeartbeat(tasks_dir=self.root, task=task, owner_agent_id='a', lease_seconds=0.3) as heartbeat:
            update_task(tasks_dir=self.root, task_id=task['id'], status='cancelled')
            self.assertTrue(heartbeat.lost.wait(2))

    def test_save_requires_loaded_revision(self):
        task = self.create()
        del task['revision']
        with self.assertRaises(TaskConflict):
            save_task(self.root, task)

    def test_completed_dependency_does_not_block(self):
        a, b = self.create(), self.create()
        update_task(tasks_dir=self.root, task_id=a['id'], status='completed')
        update_task(tasks_dir=self.root, task_id=b['id'], add_blocked_by=[a['id']])
        self.assertEqual(b['id'], self.claim()['id'])
