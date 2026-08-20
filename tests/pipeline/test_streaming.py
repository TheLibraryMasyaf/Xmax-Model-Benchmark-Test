from __future__ import annotations

import threading
import unittest

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
        self.assertLess(
            events.index("evaluate_start_run-1"), events.index("generate_end_2")
        )
        self.assertTrue(
            outcome.metadata["overlap_observed"][
                "evaluate_before_generation_finished"
            ]
        )

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


if __name__ == "__main__":
    unittest.main()
