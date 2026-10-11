import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.runtime.history import AtomicSQLiteSession, HistoryConflict
from src.context.budget import govern_input, complete_tool_pairs, ContextBudgetExceeded, count_tokens


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = AtomicSQLiteSession('test', self.root / 'history.db')

    async def test_protocol_and_reopen(self):
        items = [{'role': 'user', 'content': 'one'}, {'role': 'assistant', 'content': 'two'}]
        await self.session.add_items(items)
        self.assertEqual(items[1:], await self.session.get_items(limit=1))
        self.assertEqual(items[1], await self.session.pop_item())
        reopened = AtomicSQLiteSession('test', self.session.db_path)
        self.assertEqual(items[:1], await reopened.get_items())
        await self.session.clear_session()
        self.assertEqual([], await reopened.get_items())

    async def test_replace_is_atomic_and_fenced(self):
        await self.session.add_items([{'role': 'user', 'content': 'original'}])
        _, revision = await self.session.snapshot()
        await self.session.add_items([{'role': 'user', 'content': 'concurrent'}])
        with self.assertRaises(HistoryConflict):
            await self.session.replace_items([{'role': 'system', 'content': 'summary'}], expected_revision=revision)
        self.assertEqual(2, len(await self.session.get_items()))
        _, revision = await self.session.snapshot()
        await self.session.replace_items([{'role': 'system', 'content': 'summary'}], expected_revision=revision)
        self.assertEqual('summary', (await self.session.get_items())[0]['content'])

    async def test_invalid_replacement_keeps_history(self):
        await self.session.add_items([{'role': 'user', 'content': 'original'}])
        _, revision = await self.session.snapshot()
        with self.assertRaises(TypeError):
            await self.session.replace_items([object()], expected_revision=revision)
        self.assertEqual('original', (await self.session.get_items())[0]['content'])

    async def test_legacy_sdk_migration(self):
        path = self.root / 'legacy.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE agent_messages (id INTEGER PRIMARY KEY, session_id TEXT, message_data TEXT)')
            db.execute('INSERT INTO agent_messages VALUES (1, ?, ?)', ('old', json.dumps({'role': 'user', 'content': 'legacy'})))
        legacy = AtomicSQLiteSession('old', path)
        self.assertEqual('legacy', (await legacy.get_items())[0]['content'])
        await legacy.clear_session()
        self.assertEqual([], await AtomicSQLiteSession('old', path).get_items())

    async def test_tool_output_budget_preserves_goal_and_pairs(self):
        items = [{'role': 'user', 'content': 'keep this exact goal'},
                 {'type': 'function_call', 'call_id': 'c', 'name': 'Bash', 'arguments': '{}'},
                 {'type': 'function_call_output', 'call_id': 'c', 'output': 'x' * 30000}]
        result = await govern_input(items, budget=1600, archive_dir=self.root)
        self.assertEqual(items[0], result[0])
        self.assertEqual(3, len(complete_tool_pairs(result)))
        self.assertEqual('x' * 30000, items[2]['output'])
        archive = next(self.root.glob('model-input-*.json'))
        self.assertEqual(items, json.loads(archive.read_text()))

    async def test_schema_and_instruction_overhead_enforced(self):
        with self.assertRaises(ContextBudgetExceeded):
            await govern_input([{'role': 'user', 'content': 'goal'}], instructions='x' * 10000,
                               schemas=[{'parameters': 'y' * 10000}], budget=512)

    async def test_history_summary_preserves_current_request(self):
        class Summary:
            def to_message_text(self): return 'Keep earlier constraint: Python 3.12.'
        async def summarize(items, model): return Summary()
        items = [{'role': 'user', 'content': 'old ' * 250}, {'role': 'assistant', 'content': 'answer ' * 100},
                 {'role': 'user', 'content': 'current exact request'}]
        result = await govern_input(items, budget=650, summary_generator=summarize, archive_dir=self.root)
        self.assertEqual(items[-1], result[-1])
        self.assertIn('Python 3.12', result[0]['content'])

    async def test_orphaned_calls_and_outputs_removed(self):
        items = [{'type': 'function_call', 'call_id': 'a'}, {'type': 'function_call_output', 'call_id': 'b'},
                 {'role': 'user', 'content': 'goal'}]
        self.assertEqual(items[-1:], complete_tool_pairs(items))
