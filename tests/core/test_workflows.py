import tempfile
import unittest
from pathlib import Path

from src.tasks.task_graph import claim_task, fail_task_attempt, update_task
from src.tasks.task_store import TaskConflict, get_task
from src.tasks.workflow import (
    activate_workflow,
    define_workflow,
    dependency_results,
    get_workflow_run,
    start_workflow,
)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def define(self, steps, name="code-review"):
        return define_workflow(tasks_dir=self.root, name=name, steps=steps)

    def test_rejects_dependency_cycle_and_duplicate_name(self):
        with self.assertRaisesRegex(ValueError, "循环"):
            self.define([
                {"step_id": "a", "title": "A", "prompt": "A", "role": "coder", "depends_on": ["b"]},
                {"step_id": "b", "title": "B", "prompt": "B", "role": "tester", "depends_on": ["a"]},
            ])
        steps = [{"step_id": "a", "title": "A", "prompt": "A", "role": "coder"}]
        self.define(steps)
        with self.assertRaises(TaskConflict):
            self.define(steps)

    def test_fixed_agent_cannot_have_conflicting_roles(self):
        with self.assertRaisesRegex(ValueError, "多个角色"):
            self.define([
                {"step_id": "a", "title": "A", "prompt": "A", "agent": "alice", "role": "coder"},
                {"step_id": "b", "title": "B", "prompt": "B", "agent": "alice", "role": "tester"},
            ])

    def test_workflow_routes_tasks_and_passes_upstream_results(self):
        definition = self.define([
            {"step_id": "implement", "title": "Implement", "prompt": "Make the change", "role": "coder"},
            {
                "step_id": "test",
                "title": "Test",
                "prompt": "Verify the change",
                "role": "tester",
                "depends_on": ["implement"],
            },
        ])
        result = start_workflow(tasks_dir=self.root, workflow_id=definition["id"])
        tasks = {task["workflow_step_id"]: task for task in result["tasks"]}
        implementation = tasks["implement"]
        testing = tasks["test"]
        self.assertEqual([implementation["id"]], testing["depends_on"])
        self.assertEqual([implementation["id"]], testing["input_from"])
        self.assertEqual([implementation["id"]], testing["blockedBy"])

        self.assertIsNone(claim_task(
            tasks_dir=self.root, owner_agent_id="wrong", owner="wrong", lease_seconds=30,
            agent_name="test-agent", agent_role="tester",
        ))
        claimed_implementation = claim_task(
            tasks_dir=self.root, owner_agent_id="coder-id", owner="teammate:coder", lease_seconds=30,
            agent_name="coder-agent", agent_role="coder",
        )
        self.assertEqual(implementation["id"], claimed_implementation["id"])
        update_task(
            tasks_dir=self.root,
            task_id=implementation["id"],
            status="completed",
            result_summary="Updated checkout.py and added a regression test.",
            expected_owner_agent_id="coder-id",
            expected_lease_token=claimed_implementation["lease_token"],
        )

        current_test = get_task(self.root, testing["id"])
        self.assertEqual([], current_test["blockedBy"])
        outputs = dependency_results(tasks_dir=self.root, task=current_test)
        self.assertEqual("Updated checkout.py and added a regression test.", outputs[0]["result_summary"])
        claimed_test = claim_task(
            tasks_dir=self.root, owner_agent_id="tester-id", owner="teammate:tester", lease_seconds=30,
            agent_name="tester-agent", agent_role="tester",
        )
        self.assertEqual(testing["id"], claimed_test["id"])
        update_task(
            tasks_dir=self.root,
            task_id=testing["id"],
            status="completed",
            result_summary="All tests passed.",
            expected_owner_agent_id="tester-id",
            expected_lease_token=claimed_test["lease_token"],
        )
        current_run = get_workflow_run(tasks_dir=self.root, run_id=result["run"]["id"])["run"]
        self.assertEqual("completed", current_run["status"])

    def test_only_named_agent_can_claim_assigned_step(self):
        definition = self.define([
            {"step_id": "review", "title": "Review", "prompt": "Review changes", "agent": "alice"},
        ])
        task = start_workflow(tasks_dir=self.root, workflow_id=definition["id"])["tasks"][0]
        self.assertIsNone(claim_task(
            tasks_dir=self.root, owner_agent_id="bob-id", owner="teammate:bob", lease_seconds=30,
            agent_name="bob", agent_role="general",
        ))
        claimed = claim_task(
            tasks_dir=self.root, owner_agent_id="alice-id", owner="teammate:alice", lease_seconds=30,
            agent_name="alice", agent_role="general",
        )
        self.assertEqual(task["id"], claimed["id"])

    def test_preparing_workflow_cannot_be_claimed_until_activated(self):
        definition = self.define([
            {"step_id": "build", "title": "Build", "prompt": "Build it", "role": "coder"},
        ])
        result = start_workflow(tasks_dir=self.root, workflow_id=definition["id"], activate=False)
        kwargs = {
            "tasks_dir": self.root,
            "owner_agent_id": "coder-id",
            "owner": "teammate:coder",
            "lease_seconds": 30,
            "agent_role": "coder",
        }
        self.assertIsNone(claim_task(**kwargs))
        activate_workflow(tasks_dir=self.root, run_id=result["run"]["id"])
        self.assertIsNotNone(claim_task(**kwargs))

    def test_worktree_workflow_serializes_ready_steps(self):
        definition = self.define([
            {
                "step_id": "first",
                "title": "First",
                "prompt": "Make first change",
                "role": "coder",
                "require_worktree": True,
            },
            {
                "step_id": "second",
                "title": "Second",
                "prompt": "Make second change",
                "role": "tester",
            },
        ])
        result = start_workflow(tasks_dir=self.root, workflow_id=definition["id"])
        self.assertTrue(result["run"]["require_worktree"])
        self.assertTrue(all(task["require_worktree"] for task in result["tasks"]))
        first = claim_task(
            tasks_dir=self.root, owner_agent_id="coder-id", owner="teammate:coder", lease_seconds=30,
            agent_role="coder",
        )
        second = claim_task(
            tasks_dir=self.root, owner_agent_id="tester-id", owner="teammate:tester", lease_seconds=30,
            agent_role="tester",
        )
        self.assertEqual("first", first["workflow_step_id"])
        self.assertIsNone(second)

    def test_failed_attempt_retries_then_marks_run_failed(self):
        definition = self.define([
            {
                "step_id": "build",
                "title": "Build",
                "prompt": "Build the feature",
                "role": "coder",
                "max_retries": 1,
                "retry_delay_seconds": 0,
            },
        ])
        result = start_workflow(tasks_dir=self.root, workflow_id=definition["id"])
        task_id = result["tasks"][0]["id"]
        first = claim_task(
            tasks_dir=self.root, owner_agent_id="coder-id", owner="teammate:coder", lease_seconds=30,
            agent_name="coder", agent_role="coder",
        )
        retry = fail_task_attempt(
            tasks_dir=self.root, task_id=task_id, error="temporary model error",
            expected_owner_agent_id="coder-id", expected_lease_token=first["lease_token"],
        )
        self.assertEqual("pending", retry["status"])
        self.assertEqual(1, retry["attempts"])

        second = claim_task(
            tasks_dir=self.root, owner_agent_id="coder-id", owner="teammate:coder", lease_seconds=30,
            agent_name="coder", agent_role="coder",
        )
        failed = fail_task_attempt(
            tasks_dir=self.root, task_id=task_id, error="permanent model error",
            expected_owner_agent_id="coder-id", expected_lease_token=second["lease_token"],
        )
        self.assertEqual("failed", failed["status"])
        self.assertEqual("failed", get_workflow_run(tasks_dir=self.root, run_id=result["run"]["id"])["run"]["status"])


if __name__ == "__main__":
    unittest.main()
