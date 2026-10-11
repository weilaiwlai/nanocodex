import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.core.sdk_stub import install_if_missing
install_if_missing()

from src.runtime.config import RuntimeConfig
from src.runtime.session import build_cli_session_runtime
from src.runtime.runner import run_events
from src.context.budget import build_model_input_filter
from src.context.compaction import compact_session_history, HistorySummary
from src.tasks.agent_team import AgentTeamRuntime, TeammateWorker
from src.tasks.task_store import create_task
import queue


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'repo'
        self.workspace.mkdir()
        self.runtime = build_cli_session_runtime(session_root=self.root / 'sessions', new_session=True,
                                                 workspace_root=self.workspace, trace_enabled=False)
        self.addCleanup(self.runtime.close)

    async def test_root_stream_config_and_live_events(self):
        observed = {}
        context = self.runtime.context
        class Result:
            final_output = 'done'
            context_wrapper = SimpleNamespace(usage=SimpleNamespace(input_tokens=30, output_tokens=10, total_tokens=40))
            async def stream_events(self):
                context.emit_runtime_event('command_output', {'text': 'live'})
                await asyncio.sleep(0.1)
                if False: yield None
            def cancel(self): pass
        def run(agent, **kwargs):
            observed.update(kwargs)
            return Result()
        with patch('src.runtime.runner.configure_openai_runtime'), patch('src.runtime.runner.Runner.run_streamed', side_effect=run):
            events = [e async for e in run_events('goal', RuntimeConfig('fake', 'test-model', 'test-model'), self.runtime)]
        self.assertIn('command_output', [e['type'] for e in events])
        self.assertEqual('run_finished', events[-1]['type'])
        self.assertEqual(40, events[-1]['payload']['usage']['total_tokens'])
        self.assertTrue(callable(observed['run_config'].call_model_input_filter))

    async def test_actual_filter_adapter_shape(self):
        model_data = SimpleNamespace(input=[{'role': 'user', 'content': 'exact goal'}], instructions='rules')
        result = await build_model_input_filter(self.runtime.context, 'test-model')(
            SimpleNamespace(model_data=model_data, agent=SimpleNamespace(tools=[])))
        self.assertEqual(model_data.input, result.input)
        self.assertEqual('rules', result.instructions)

    async def test_repository_rules_cannot_follow_external_symlink(self):
        from src.context.context_builder import build_repo_rule_layer
        private = self.root / 'private.txt'
        private.write_text('private content')
        (self.workspace / 'code_law.md').symlink_to(private)
        self.assertFalse(build_repo_rule_layer(workspace_root=self.workspace).available)

    async def test_compaction_transaction(self):
        await self.runtime.session.add_items([{'role': 'user', 'content': 'old goal'}])
        async def summarize(items, model):
            return HistorySummary(layer='L3', current_goal='old goal', key_constraints_and_decisions=[],
                                  important_files_and_evidence=[], unfinished_items=[])
        from dataclasses import replace
        from src.context.compaction import get_context_compaction_config
        result = await compact_session_history(session=self.runtime.session, session_id=self.runtime.session_id,
            model='test-model', summary_generator=summarize, force=True,
            config=replace(get_context_compaction_config(), archive_dir=self.root / 'archive'))
        self.assertTrue(result.compacted)
        self.assertTrue((self.root / 'archive').exists())
        self.assertIn('Archived Session Summary', (await self.runtime.session.get_items())[0]['content'])

    async def test_summarizer_splits_oversized_history(self):
        import json
        from src.context.compaction import generate_history_summary
        from src.context.budget import count_tokens, input_budget
        observed = []
        async def run(agent, **kwargs):
            observed.append(count_tokens(agent.instructions + kwargs['input'], 'test-model'))
            return SimpleNamespace(final_output=json.dumps({'current_goal': 'keep the goal',
                'key_constraints_and_decisions': [], 'important_files_and_evidence': [], 'unfinished_items': []}))
        with patch('src.context.compaction.Runner.run', side_effect=run):
            result = await generate_history_summary([{'role': 'user', 'content': 'context ' * 30000}], 'test-model')
        self.assertEqual('keep the goal', result.current_goal)
        self.assertGreater(len(observed), 1)
        self.assertTrue(all(tokens < input_budget() for tokens in observed))

    async def test_teammate_uses_root_context_and_budget(self):
        from src.runtime.history import AtomicSQLiteSession
        team = AgentTeamRuntime(session_id=self.runtime.session_id, session_name='test',
                                team_dir=self.runtime.context.team_dir, base_context=self.runtime.context)
        session = AtomicSQLiteSession('worker', self.root / 'worker.db')
        context = team._build_worker_context(name='worker', agent_id='worker', sdk_session=session)
        worker = TeammateWorker(agent_id='worker', name='worker', role='implement', prompt='',
                                transcript_path=self.root / 'transcript.jsonl', recent_transcript_path=self.root / 'recent.json',
                                context=context, sdk_session=session, message_queue=queue.Queue())
        observed = {}
        async def run(agent, **kwargs):
            observed['agent'] = agent
            observed.update(kwargs)
            return SimpleNamespace(final_output='done')
        with patch('src.tasks.agent_team.Runner.run', side_effect=run):
            await team._run_worker_turn(worker, {'role': 'user', 'content': 'worker goal'})
        self.assertIn('Operating Principles', observed['agent'].instructions)
        self.assertTrue(callable(observed['run_config'].call_model_input_filter))
        self.assertIs(context.permission_broker, self.runtime.context.permission_broker)
        self.assertIs(context.command_manager, self.runtime.context.command_manager)
