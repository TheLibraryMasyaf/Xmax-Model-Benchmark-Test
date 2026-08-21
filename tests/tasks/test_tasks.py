from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.tasks import TaskAllocator, TaskWorker
from xmax_test.time import FixedClock

ROOT = Path(__file__).resolve().parents[2]


class TaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FixedClock()
        self.repository = SqliteMetadataRepository(
            Path(self.temp.name) / "db.sqlite3", clock=self.clock
        )
        self.plan = {
            "plan_id": "plan-test",
            "plan_hash": "a" * 64,
            "cases": [
                {"case_id": "case-a", "case_number": "feed001_prompt001"},
                {"case_id": "case-b", "case_number": "feed001_prompt002"},
            ],
        }
        self.batch = TaskAllocator(self.repository, self.clock).create(self.plan)

    def tearDown(self) -> None:
        self.repository.close()
        self.temp.cleanup()

    def test_tasks_pass_schema_and_are_frozen(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "test-task.schema.json").read_text(encoding="utf-8")
        )
        tasks = self.repository.list_test_tasks(task_batch_id=self.batch["task_batch_id"])
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            Draft202012Validator(schema).validate(task)
            self.assertEqual(task["status"], "pending")
            self.assertIn("case", task["payload"])

    def test_claim_is_exclusive_and_expired_lease_is_reclaimed(self) -> None:
        first = self.repository.claim_next_test_task(
            self.batch["task_batch_id"], "worker-a", lease_seconds=10
        )
        second = self.repository.claim_next_test_task(
            self.batch["task_batch_id"], "worker-b", lease_seconds=10
        )
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertIsNone(
            self.repository.claim_next_test_task(
                self.batch["task_batch_id"], "worker-c", lease_seconds=10
            )
        )
        self.clock.advance(11)
        reclaimed = self.repository.claim_next_test_task(
            self.batch["task_batch_id"], "worker-c", lease_seconds=10
        )
        self.assertIn(reclaimed["task_id"], {first["task_id"], second["task_id"]})
        self.assertEqual(reclaimed["lease_owner"], "worker-c")

    def test_worker_continues_after_error_and_can_resume_only_failed_task(self) -> None:
        failing_case = "case-a"

        def execute(task):
            if task["case_id"] == failing_case:
                raise RuntimeError("boom")
            return {"run_id": "run-ok"}

        worker = TaskWorker(self.repository, execute)
        summary = worker.run_batch(self.batch["task_batch_id"], lease_owner="worker-a")
        self.assertEqual(summary["counts"], {"completed": 1, "error": 1})
        failed = self.repository.list_test_tasks(status="error")[0]

        resumed = TaskWorker(self.repository, lambda task: {"run_id": "run-retried"}).run_task(
            failed["task_id"], lease_owner="worker-b", resume=True
        )
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["result_refs"]["run_id"], "run-retried")
        self.assertEqual(
            self.repository.test_task_summary(self.batch["task_batch_id"])["counts"],
            {"completed": 2},
        )

    def test_batch_preflight_failure_claims_no_tasks(self) -> None:
        class BrokenRuntime:
            def preflight(self, tasks):
                self.seen = len(tasks)
                raise RuntimeError("shared transport unavailable")

            def __call__(self, task):  # pragma: no cover - must never execute
                raise AssertionError("task execution must not start")

        runtime = BrokenRuntime()
        with self.assertRaisesRegex(RuntimeError, "shared transport unavailable"):
            TaskWorker(self.repository, runtime).run_batch(
                self.batch["task_batch_id"], lease_owner="worker-a"
            )
        self.assertEqual(runtime.seen, 2)
        self.assertEqual(
            self.repository.test_task_summary(self.batch["task_batch_id"])["counts"],
            {"pending": 2},
        )

    def test_completed_task_is_idempotently_reused(self) -> None:
        task_id = self.batch["task_ids"][0]
        calls = []
        worker = TaskWorker(
            self.repository,
            lambda task: calls.append(task["task_id"]) or {"run_id": "run-1"},
        )
        worker.run_task(task_id, lease_owner="worker-a")
        second = worker.run_task(task_id, lease_owner="worker-a")
        self.assertEqual(calls, [task_id])
        self.assertTrue(second["reused"])

    def test_explicit_claim_can_be_executed_by_same_owner(self) -> None:
        claimed = self.repository.claim_next_test_task(self.batch["task_batch_id"], "scheduler-a")
        result = TaskWorker(self.repository, lambda task: {"run_id": "run-after-claim"}).run_task(
            claimed["task_id"], lease_owner="scheduler-a"
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result_refs"]["run_id"], "run-after-claim")


if __name__ == "__main__":
    unittest.main()
