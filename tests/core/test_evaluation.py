import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate import evaluate, ROOT


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_eval(self, argv, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return evaluate(tasks_path=ROOT / 'evals' / 'coding_tasks.json', agent_argv=argv,
                            output_dir=self.root, **kwargs)

    def test_repeated_known_answers_and_report(self):
        report = self.run_eval([sys.executable, '{project}/evals/smoke_agent.py'], repeat=2)
        self.assertEqual(6, report['passed'])
        self.assertEqual(1, report['completion_rate'])
        self.assertIsNone(report['results'][0]['tokens'])
        self.assertIsNone(report['results'][0]['cost_usd'])
        self.assertTrue((self.root / 'report.json').exists())

    def test_unmodified_fixture_fails(self):
        report = self.run_eval([sys.executable, '-c', 'pass'])
        self.assertEqual(0, report['passed'])
        self.assertEqual(3, report['total'])

    def test_timeout_and_usage_accounting(self):
        report = self.run_eval([sys.executable, '-c', 'import time; time.sleep(3)'], timeout=0.05)
        self.assertTrue(all(result['timed_out'] for result in report['results']))
        self.assertEqual(0, report['passed'])
