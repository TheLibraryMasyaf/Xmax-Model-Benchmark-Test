from __future__ import annotations

import unittest

from xmax_test.evaluation.group_metrics import BatchGroupEvaluator


class CaseRepository:
    def __init__(self, cases: dict[str, dict]) -> None:
        self.cases = cases

    def get_test_case(self, case_id: str) -> dict:
        return self.cases[case_id]


class BatchGroupEvaluatorTests(unittest.TestCase):
    def test_repeat_transfer_and_cross_input_scores_use_the_frozen_batch(self) -> None:
        cases = {
            "case-a1": self.case("feed-a", 1),
            "case-a2": self.case("feed-a", 2),
            "case-b1": self.case("feed-b", 1),
        }
        runs = [
            self.run_record("run-a1", "case-a1"),
            self.run_record("run-a2", "case-a2"),
            self.run_record("run-b1", "case-b1"),
        ]
        results = [self.result(run["run_id"]) for run in runs]

        judgments = BatchGroupEvaluator(CaseRepository(cases)).judgments(runs, results)
        by_dimension = {item["dimension_id"]: item for item in judgments["run-a1"]}
        self.assertEqual(self.criterion(by_dimension["C1"], "C1.2")["score"], 2.0)
        self.assertEqual(self.criterion(by_dimension["O4"], "O4.1")["score"], 2.0)
        self.assertEqual(self.criterion(by_dimension["O4"], "O4.2")["score"], 2.0)
        self.assertEqual(self.criterion(by_dimension["O5"], "O5.2")["score"], 2.0)
        self.assertEqual(self.criterion(by_dimension["O5"], "O5.3")["score"], 2.0)

    def test_single_run_stability_is_explicit_not_applicable(self) -> None:
        cases = {"case-a1": self.case("feed-a", 1)}
        runs = [self.run_record("run-a1", "case-a1")]
        results = [self.result("run-a1")]

        judgments = BatchGroupEvaluator(CaseRepository(cases)).judgments(runs, results)
        by_dimension = {item["dimension_id"]: item for item in judgments["run-a1"]}
        for criterion_id in ("O4.1", "O4.2"):
            criterion = self.criterion(by_dimension["O4"], criterion_id)
            self.assertFalse(criterion["applicable"])
            self.assertIsNone(criterion["score"])
            self.assertEqual(criterion["raw_metrics"]["member_run_ids"], ["run-a1"])
        c1 = self.criterion(by_dimension["C1"], "C1.2")
        self.assertFalse(c1["applicable"])
        self.assertEqual(c1["raw_metrics"]["member_run_ids"], ["run-a1"])

    def test_results_outside_frozen_run_set_are_rejected(self) -> None:
        cases = {"case-a1": self.case("feed-a", 1)}
        runs = [self.run_record("run-a1", "case-a1")]
        with self.assertRaisesRegex(ValueError, "outside the frozen Run set"):
            BatchGroupEvaluator(CaseRepository(cases)).judgments(
                runs, [self.result("run-a1"), self.result("run-old-history")]
            )

    def test_group_criteria_publish_aggregation_scope(self) -> None:
        cases = {"case-a1": self.case("feed-a", 1)}
        judgments = BatchGroupEvaluator(CaseRepository(cases)).judgments(
            [self.run_record("run-a1", "case-a1")], [self.result("run-a1")]
        )
        by_dimension = {item["dimension_id"]: item for item in judgments["run-a1"]}
        self.assertEqual(
            self.criterion(by_dimension["C1"], "C1.2")["aggregation_scope"], "batch"
        )
        self.assertEqual(
            self.criterion(by_dimension["O4"], "O4.1")["aggregation_scope"],
            "repeat_group",
        )

    @staticmethod
    def case(feed: str, repeat: int) -> dict:
        return {
            "feed_asset_id": feed,
            "prompt_asset_ids": ["prompt-video"],
            "prompt_text": "replace dance",
            "operation_recipe_id": "dance-replace",
            "scenario_id": "core-dance",
            "repeat_index": repeat,
        }

    @staticmethod
    def run_record(run_id: str, case_id: str) -> dict:
        return {
            "run_id": run_id,
            "case_id": case_id,
            "model_id": "xmax-test-version",
            "mode": "offline",
            "status": "completed",
            "metrics": {"retry_count": 0, "credits": 1},
        }

    @staticmethod
    def result(run_id: str) -> dict:
        return {
            "evaluation_id": f"eval-{run_id}",
            "run_id": run_id,
            "benchmark_version": "0.2.0-draft",
            "criterion_results": [{"dimension_id": "C2", "criterion_id": "C2.1", "score": 2.0}],
        }

    @staticmethod
    def criterion(judgment: dict, criterion_id: str) -> dict:
        return next(
            item for item in judgment["criterion_results"] if item["criterion_id"] == criterion_id
        )


if __name__ == "__main__":
    unittest.main()
