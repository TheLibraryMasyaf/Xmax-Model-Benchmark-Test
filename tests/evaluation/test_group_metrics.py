from __future__ import annotations

import unittest

from xmax_test.evaluation.group_metrics import BatchReportingMetrics


class CaseRepository:
    def __init__(self, cases: dict[str, dict]) -> None:
        self.cases = cases

    def get_test_case(self, case_id: str) -> dict:
        return self.cases[case_id]


class BatchReportingMetricsTests(unittest.TestCase):
    def test_repeat_and_generation_metrics_are_report_only(self) -> None:
        cases = {"case-a1": self.case("feed-a", 1), "case-a2": self.case("feed-a", 2), "case-b1": self.case("feed-b", 1)}
        runs = [self.run_record("run-a1", "case-a1"), self.run_record("run-a2", "case-a2"), self.run_record("run-b1", "case-b1")]
        summary = BatchReportingMetrics(CaseRepository(cases)).summarize(runs, [self.result(item["run_id"]) for item in runs])
        self.assertEqual(summary["P.2"]["valid_result_count"], 3)
        self.assertEqual(summary["P.3"]["repeated_group_count"], 1)
        repeated = next(item for item in summary["P.3"]["groups"] if item["configured_repeat_count"] == 2)
        self.assertEqual(repeated["valid_result_rate_percent"], 100.0)
        self.assertNotIn("score", summary["P.2"])
        self.assertNotIn("score", repeated)

    def test_single_run_is_reported_as_non_repeated_group(self) -> None:
        cases = {"case-a1": self.case("feed-a", 1)}
        summary = BatchReportingMetrics(CaseRepository(cases)).summarize([self.run_record("run-a1", "case-a1")], [self.result("run-a1")])
        self.assertEqual(summary["P.3"]["group_count"], 1)
        self.assertEqual(summary["P.3"]["repeated_group_count"], 0)

    def test_hard_gated_result_is_invalid_even_when_fused_p_score_is_nonzero(self) -> None:
        cases = {"case-a1": self.case("feed-a", 1)}
        result = self.result("run-a1")
        result["criterion_results"][0]["score"] = 1.0
        result["final_verdict"] = "invalid_result"
        result["applied_gate_ids"] = ["invalid-video-result"]
        summary = BatchReportingMetrics(CaseRepository(cases)).summarize(
            [self.run_record("run-a1", "case-a1")], [result]
        )
        self.assertEqual(summary["P.2"]["valid_result_count"], 0)
        self.assertEqual(summary["P.2"]["invalid_output_count"], 1)

    def test_positive_fused_p_score_is_valid_when_no_hard_gate_applies(self) -> None:
        cases = {"case-a1": self.case("feed-a", 1)}
        result = self.result("run-a1")
        result["criterion_results"][0]["score"] = 1.6667
        summary = BatchReportingMetrics(CaseRepository(cases)).summarize(
            [self.run_record("run-a1", "case-a1")], [result]
        )
        self.assertEqual(summary["P.2"]["valid_result_count"], 1)
        self.assertEqual(summary["P.2"]["validity_unassessed_count"], 0)
        self.assertEqual(summary["P.3"]["groups"][0]["valid_result_count"], 1)

    def test_results_outside_frozen_run_set_are_rejected(self) -> None:
        cases = {"case-a1": self.case("feed-a", 1)}
        with self.assertRaisesRegex(ValueError, "outside the frozen Run set"):
            BatchReportingMetrics(CaseRepository(cases)).summarize([self.run_record("run-a1", "case-a1")], [self.result("run-a1"), self.result("run-old")])

    @staticmethod
    def case(feed: str, repeat: int) -> dict:
        return {"feed_asset_id": feed, "prompt_asset_ids": ["prompt-video"], "prompt_text": "replace dance", "operation_recipe_id": "dance-replace", "scenario_id": "core-high-speed-subject-edit", "repeat_index": repeat, "generation_config": {}}

    @staticmethod
    def run_record(run_id: str, case_id: str) -> dict:
        return {"run_id": run_id, "case_id": case_id, "model_id": "xmax-test-version", "mode": "offline", "status": "completed", "result_asset_id": f"asset-{run_id}", "metrics": {"retry_count": 0}}

    @staticmethod
    def result(run_id: str) -> dict:
        return {"evaluation_id": f"eval-{run_id}", "run_id": run_id, "benchmark_version": "0.3.0-draft", "case_score_percent": 100.0, "criterion_results": [{"dimension_id": "P", "criterion_id": "P.1", "score": 2.0}]}


if __name__ == "__main__":
    unittest.main()
