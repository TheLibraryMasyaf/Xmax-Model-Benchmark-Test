from __future__ import annotations

import threading
import unittest

from xmax_test.errors import EvaluationBudgetPausedError, RealtimeUnavailableError
from xmax_test.pipeline.streaming import StreamingPipelineCoordinator


class StreamingPipelineTests(unittest.TestCase):
    def test_evaluation_of_first_run_overlaps_later_generation(self) -> None:
        evaluation_started = threading.Event()
        events: list[str] = []
        lock = threading.Lock()

        def record(value: str) -> None:
            with lock:
                events.append(value)

        def generate(case):
            record(f"generate_start_{case['case_id']}")
            if case["case_id"] == "2":
                self.assertTrue(evaluation_started.wait(timeout=2))
            record(f"generate_end_{case['case_id']}")
            return {"run_id": f"run-{case['case_id']}", "status": "completed"}

        def preprocess(run):
            record(f"preprocess_{run['run_id']}")
            return {"preprocess_id": f"prep-{run['run_id']}", "run_id": run["run_id"]}

        def evaluate(run, preprocess):
            record(f"evaluate_start_{run['run_id']}")
            evaluation_started.set()
            return {"evaluation_id": f"eval-{run['run_id']}", "run_id": run["run_id"]}

        outcome = StreamingPipelineCoordinator(
            generate_case=generate,
            preprocess_run=preprocess,
            evaluate_run=evaluate,
            queue_size=1,
        ).run([{"case_id": "1"}, {"case_id": "2"}])

        self.assertEqual(len(outcome.runs), 2)
        self.assertEqual(len(outcome.preprocess), 2)
        self.assertEqual(len(outcome.evaluations), 2)
        self.assertLess(events.index("evaluate_start_run-1"), events.index("generate_end_2"))
        self.assertTrue(outcome.metadata["overlap_observed"]["evaluate_before_generation_finished"])

    def test_failed_generation_is_not_sent_downstream(self) -> None:
        preprocessed: list[str] = []

        def generate(case):
            return {
                "run_id": f"run-{case['case_id']}",
                "status": "error" if case["case_id"] == "bad" else "completed",
            }

        def preprocess(run):
            preprocessed.append(run["run_id"])
            return {"preprocess_id": f"prep-{run['run_id']}", "run_id": run["run_id"]}

        outcome = StreamingPipelineCoordinator(
            generate_case=generate,
            preprocess_run=preprocess,
            queue_size=1,
        ).run([{"case_id": "bad"}, {"case_id": "good"}])

        self.assertEqual(preprocessed, ["run-good"])
        self.assertEqual(len(outcome.errors["generate"]), 1)
        self.assertEqual(outcome.errors["preprocess"], [])

    def test_each_evaluation_is_synced_before_later_generation_finishes(self) -> None:
        synced = threading.Event()
        events: list[str] = []

        def generate(case):
            if case["case_id"] == "2":
                self.assertTrue(synced.wait(timeout=2))
            events.append(f"generated-{case['case_id']}")
            return {"run_id": f"run-{case['case_id']}", "status": "completed"}

        def preprocess(run):
            return {"run_id": run["run_id"], "preprocess_id": f"prep-{run['run_id']}"}

        def evaluate(run, preprocess):
            return {"run_id": run["run_id"], "evaluation_id": f"eval-{run['run_id']}"}

        def sync(run, evaluation):
            events.append(f"synced-{run['run_id']}")
            synced.set()
            return {"run_id": run["run_id"], "action": "created"}

        outcome = StreamingPipelineCoordinator(
            generate_case=generate,
            preprocess_run=preprocess,
            evaluate_run=evaluate,
            sync_run=sync,
            queue_size=1,
        ).run([{"case_id": "1"}, {"case_id": "2"}])

        self.assertEqual(len(outcome.sync), 2)
        self.assertLess(events.index("synced-run-1"), events.index("generated-2"))
        self.assertTrue(outcome.metadata["overlap_observed"]["sync_before_generation_finished"])

    def test_repeated_identical_generation_error_opens_circuit_breaker(self) -> None:
        calls: list[str] = []

        def generate(case):
            calls.append(case["case_id"])
            raise RuntimeError("shared transport unavailable")

        outcome = StreamingPipelineCoordinator(
            generate_case=generate,
            circuit_breaker_threshold=3,
        ).run([{"case_id": str(i)} for i in range(10)])

        self.assertEqual(calls, ["0", "1", "2"])
        self.assertTrue(outcome.metadata["circuit_breaker"]["aborted"])

    def test_realtime_unavailable_does_not_abort_remaining_plan(self) -> None:
        """RealtimeUnavailableError is a known condition, not a transient
        failure: it must be recorded per-case without tripping the circuit
        breaker, so the remaining offline cases keep draining."""

        calls: list[str] = []

        def generate(case):
            calls.append(case["case_id"])
            raise RealtimeUnavailableError(f"realtime {case['case_id']} unsupported")

        outcome = StreamingPipelineCoordinator(
            generate_case=generate,
            circuit_breaker_threshold=3,
        ).run([{"case_id": str(i)} for i in range(10)])

        self.assertEqual(calls, [str(i) for i in range(10)])
        self.assertFalse(outcome.metadata["circuit_breaker"]["aborted"])
        self.assertEqual(len(outcome.errors["generate"]), 10)
        self.assertTrue(
            all(
                item["code"] == "xmax.realtime_unavailable"
                for item in outcome.errors["generate"]
            )
        )

    def test_budget_pause_defers_evaluation_but_generation_keeps_draining(self) -> None:
        generated: list[str] = []
        evaluated: list[str] = []

        def generate(case):
            generated.append(case["case_id"])
            return {"run_id": f"run-{case['case_id']}", "status": "completed"}

        def preprocess(run):
            return {"run_id": run["run_id"], "preprocess_id": f"prep-{run['run_id']}"}

        def evaluate(run, preprocess):
            evaluated.append(run["run_id"])
            raise EvaluationBudgetPausedError("recharge required")

        outcome = StreamingPipelineCoordinator(
            generate_case=generate,
            preprocess_run=preprocess,
            evaluate_run=evaluate,
            queue_size=1,
        ).run([{"case_id": str(i)} for i in range(6)])

        self.assertEqual(generated, [str(i) for i in range(6)])
        self.assertEqual(evaluated, ["run-0"])
        self.assertEqual(len(outcome.preprocess), 6)
        self.assertEqual(len(outcome.errors["evaluate"]), 1)
        self.assertEqual(
            outcome.errors["evaluate"][0]["code"], "xmax.evaluation_budget_paused"
        )
        self.assertEqual(outcome.metadata["evaluation_gate"]["deferred_count"], 6)


if __name__ == "__main__":
    unittest.main()
