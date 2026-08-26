"""P12 Model-update reporting tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from xmax_test.benchmark import load_benchmark_contract
from xmax_test.errors import ContractError
from xmax_test.pipeline.manifests import build_batch_manifest
from xmax_test.reporting.classification import bucket_pairs, classify_pair
from xmax_test.reporting.comparison import ModelComparisonService
from xmax_test.reporting.renderer import render_markdown, template_hash
from xmax_test.reporting.service import ModelUpdateReportService
from xmax_test.reporting.single_version import (
    SingleVersionReportService,
    _credibility,
    _repeat_stability_diagnostics,
)
from xmax_test.scenarios import load_scenario_pack
from xmax_test.storage.sqlite import SqliteMetadataRepository

ROOT = Path(__file__).resolve().parents[2]

TEST_SCHEMA = {
    "score_schema_id": "test-schema",
    "version": "2.0.0",
    "status": "active",
    "dimensions": [],
    "weight_profile_by_mode": {
        "offline": "generic-offline-0.1",
        "realtime": "generic-realtime-0.1",
    },
    "case_score_output": "scenario_score",
    "comparison_policy": {
        "improvement_min_delta": 5.0,
        "tie_abs_delta_max": 2.0,
        "regression_min_delta": 5.0,
        "minimum_comparable_pairs": 1,
        "confidence_level": 0.9,
    },
}


def score_schema(benchmark: dict) -> dict:
    return TEST_SCHEMA


class ReportingTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3")
        self.benchmark = load_benchmark_contract(ROOT / "BENCHMARK.md")
        self.pack = load_scenario_pack(ROOT / "config" / "scenarios.json")
        self.template = ROOT / "report-templates" / "model-version-update-report.md"
        self.output = root / "reports"
        self._seed()

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def _seed(self) -> None:
        self.repository.upsert_test_case(
            {
                "case_id": "case-a",
                "case_number": "feed001_prompt001_01",
                "feed_number": "feed001",
                "prompt_number": "prompt001",
                "feed_asset_id": "feed-a",
                "prompt_asset_ids": [],
                "prompt_text": "换装",
                "generation_mode": "offline",
                "repeat_index": 1,
                "model_id": "x2.0",
                "operation_recipe_id": "offline-image-reference",
                "operation_recipe_version": "0.1.0",
                "edited_video_asset_id": "feed-a",
                "expected_audio_source_asset_id": "feed-a",
                "scenario_id": "core-indoor-selfie-person-replacement",
                "scenario_pack_version": self.pack.get("version"),
                "scene_tags": {"input_dimension": "自拍"},
            },
            "plan-1",
        )
        for model, score in (("x2.0", 60.0), ("x2.1", 80.0)):
            run_id = f"run-{model}"
            self.repository.create_run(
                {
                    "run_id": run_id,
                    "run_batch_id": f"batch-{model}",
                    "case_id": "case-a",
                    "case_number": "feed001_prompt001_01",
                    "status": "completed",
                    "model_id": model,
                    "mode": "offline",
                    "origin": "xmax_offline",
                    "provenance": {
                        "source_type": "t",
                        "source_locator": "l",
                        "source_hash": "h",
                    },
                    "result_asset_id": "result-a",
                    "edited_video_asset_id": "feed-a",
                    "expected_audio_source_asset_id": "feed-a",
                    "metrics": {"credits": 100},
                }
            )
            self.repository.save_evaluation_result(
                {
                    "evaluation_id": f"eval-{model}",
                    "evaluation_batch_id": f"eval-batch-{model}",
                    "run_id": run_id,
                    "benchmark_version": "0.2.0-draft",
                    "scenario_pack_version": "0.1.0-draft",
                    "score_schema_version": "2.0.0",
                    "dimension_results": [],
                    "weight_resolution": {},
                    "canonical_score": score,
                    "scenario_score": score,
                    "case_score_percent": score,
                    "applied_gate_ids": [],
                    "final_verdict": None,
                }
            )
            self.repository.save_batch_manifest(
                build_batch_manifest(
                    entity_type="run_batch",
                    item_entity_type="generation_run",
                    item_ids=[run_id],
                    producer_stage_run_id="stage-test",
                    batch_id=f"batch-{model}",
                )
            )
            self.repository.save_batch_manifest(
                build_batch_manifest(
                    entity_type="evaluation_batch",
                    item_entity_type="evaluation_result",
                    item_ids=[f"eval-{model}"],
                    producer_stage_run_id="stage-test",
                    batch_id=f"eval-batch-{model}",
                )
            )
        for entity_type, batch_id, item_type in (
            ("run_batch", "batch-x9.9", "generation_run"),
            ("evaluation_batch", "eval-batch-x9.9", "evaluation_result"),
        ):
            self.repository.save_batch_manifest(
                build_batch_manifest(
                    entity_type=entity_type,
                    item_entity_type=item_type,
                    item_ids=[],
                    producer_stage_run_id="stage-test",
                    batch_id=batch_id,
                )
            )

    @staticmethod
    def selectors(baseline: str = "x2.0", candidate: str = "x2.1") -> dict:
        return {
            "baseline_run_batch_id": f"batch-{baseline}",
            "candidate_run_batch_id": f"batch-{candidate}",
            "baseline_evaluation_batch_id": f"eval-batch-{baseline}",
            "candidate_evaluation_batch_id": f"eval-batch-{candidate}",
        }

    def service(self) -> ModelUpdateReportService:
        return ModelUpdateReportService(
            ReportRepositoryProxy(self.repository),
            ModelComparisonService(self.repository, self.benchmark, self.pack, TEST_SCHEMA),
            self.benchmark,
            self.pack,
            TEST_SCHEMA,
        )


class ReportRepositoryProxy:
    def __init__(self, repository) -> None:
        self._repository = repository

    def runs_for_model(self, model_version: str) -> list[dict]:
        return self._repository.list_runs(model_id=model_version)

    def evaluation_for_run(self, run_id: str) -> dict | None:
        results = self._repository.list_evaluation_results(run_id=run_id)
        return results[0] if results else None

    def case(self, case_id: str) -> dict:
        return self._repository.get_test_case(case_id)

    def batch_manifest(self, entity_type: str, batch_id: str) -> dict:
        return self._repository.get_batch_manifest(entity_type, batch_id)

    def human_signals(self) -> list[dict]:
        return self._repository.list_human_signals()

    def evaluation_overrides(self) -> list[dict]:
        return self._repository.list_evaluation_overrides()


class ComparisonTests(ReportingTestBase):
    def test_comparison_ignores_evaluation_rows_not_in_frozen_manifest(self) -> None:
        self.repository.save_evaluation_result(
            {
                "evaluation_id": "eval-x2.0-dirty-history",
                "evaluation_batch_id": "eval-batch-x2.0",
                "run_id": "run-x2.0",
                "benchmark_version": "old",
                "dimension_results": [],
                "criterion_results": [],
                "canonical_score": 5.0,
                "scenario_score": 5.0,
                "case_score_percent": 5.0,
                "applied_gate_ids": [],
                "final_verdict": None,
            }
        )

        compared = ModelComparisonService(
            self.repository, self.benchmark, self.pack, TEST_SCHEMA
        ).compare(
            baseline_model_version="x2.0",
            candidate_model_version="x2.1",
            **self.selectors(),
            requested_scene_ids=["core-indoor-selfie-person-replacement"],
        )

        self.assertEqual(compared["overall"]["canonical"]["delta_points"], 20.0)

    def test_paired_samples_compute_dual_score_deltas(self) -> None:
        compared = ModelComparisonService(
            self.repository, self.benchmark, self.pack, TEST_SCHEMA
        ).compare(
            baseline_model_version="x2.0",
            candidate_model_version="x2.1",
            **self.selectors(),
            requested_scene_ids=["core-indoor-selfie-person-replacement"],
        )
        self.assertEqual(compared["status"], "complete")
        self.assertTrue(compared["comparability"]["comparable"])
        self.assertEqual(len(compared["pairs"]), 1)
        overall = compared["overall"]
        self.assertEqual(overall["canonical"]["delta_points"], 20.0)
        self.assertEqual(overall["scenario"]["delta_points"], 20.0)

    def test_missing_pairs_are_not_comparable(self) -> None:
        compared = ModelComparisonService(
            self.repository, self.benchmark, self.pack, TEST_SCHEMA
        ).compare(
            baseline_model_version="x2.0",
            candidate_model_version="x9.9",
            **self.selectors(candidate="x9.9"),
            requested_scene_ids=["core-indoor-selfie-person-replacement"],
        )
        self.assertEqual(compared["status"], "not_comparable")
        self.assertFalse(compared["comparability"]["comparable"])

    def test_different_generation_config_is_not_paired(self) -> None:
        case = self.repository.get_test_case("case-a")
        case = {
            **case,
            "case_id": "case-b",
            "model_id": "x2.2",
            "generation_config": {"quality": "sd", "fps": 24},
        }
        self.repository.upsert_test_case(case, "plan-2")
        self.repository.create_run(
            {
                "run_id": "run-x2.2",
                "run_batch_id": "batch-x2.2",
                "case_id": "case-b",
                "case_number": case["case_number"],
                "status": "completed",
                "model_id": "x2.2",
                "mode": "offline",
                "origin": "xmax_offline",
                "provenance": {
                    "source_type": "t",
                    "source_locator": "l",
                    "source_hash": "h2",
                },
                "result_asset_id": "result-b",
                "edited_video_asset_id": "feed-a",
                "expected_audio_source_asset_id": "feed-a",
                "metrics": {},
            }
        )
        self.repository.save_evaluation_result(
            {
                "evaluation_id": "eval-x2.2",
                "evaluation_batch_id": "eval-batch-x2.2",
                "run_id": "run-x2.2",
                "benchmark_version": "0.2.0-draft",
                "scenario_pack_version": "0.1.0-draft",
                "score_schema_version": "2.0.0",
                "dimension_results": [],
                "weight_resolution": {},
                "canonical_score": 80.0,
                "scenario_score": 80.0,
                "case_score_percent": 80.0,
                "applied_gate_ids": [],
                "final_verdict": None,
            }
        )
        self.repository.save_batch_manifest(
            build_batch_manifest(
                entity_type="run_batch",
                item_entity_type="generation_run",
                item_ids=["run-x2.2"],
                producer_stage_run_id="stage-test",
                batch_id="batch-x2.2",
            )
        )
        self.repository.save_batch_manifest(
            build_batch_manifest(
                entity_type="evaluation_batch",
                item_entity_type="evaluation_result",
                item_ids=["eval-x2.2"],
                producer_stage_run_id="stage-test",
                batch_id="eval-batch-x2.2",
            )
        )
        compared = ModelComparisonService(
            self.repository, self.benchmark, self.pack, TEST_SCHEMA
        ).compare(
            baseline_model_version="x2.0",
            candidate_model_version="x2.2",
            **self.selectors(candidate="x2.2"),
            requested_scene_ids=["core-indoor-selfie-person-replacement"],
        )
        self.assertEqual(compared["status"], "not_comparable")
        self.assertIn(
            "unpaired_cases",
            {item["kind"] for item in compared["comparability"]["differences"]},
        )

    def test_different_score_basis_is_excluded_from_comparison(self) -> None:
        criterion_id = self.benchmark["dimensions"][0]["criteria"][0]["criterion_id"]
        for model, applicable in (("x2.0", True), ("x2.1", False)):
            evaluation_id = f"eval-basis-{model}"
            evaluation_batch_id = f"eval-batch-basis-{model}"
            self.repository.save_evaluation_result(
                {
                    "evaluation_id": evaluation_id,
                    "evaluation_batch_id": evaluation_batch_id,
                    "run_id": f"run-{model}",
                    "benchmark_version": "0.2.0-draft",
                    "scenario_pack_version": "0.1.0-draft",
                    "score_schema_version": "2.0.0",
                    "criterion_results": [
                        {
                            "criterion_id": criterion_id,
                            "score": 1.0 if applicable else None,
                            "applicable": applicable,
                        }
                    ],
                    "dimension_results": [],
                    "weight_resolution": {"effective_weights": {"G1": 1.0}},
                    "canonical_score": 60.0 if model == "x2.0" else 80.0,
                    "scenario_score": 60.0 if model == "x2.0" else 80.0,
                    "case_score_percent": 60.0 if model == "x2.0" else 80.0,
                    "applied_gate_ids": [],
                    "final_verdict": None,
                }
            )
            self.repository.save_batch_manifest(
                build_batch_manifest(
                    entity_type="evaluation_batch",
                    item_entity_type="evaluation_result",
                    item_ids=[evaluation_id],
                    producer_stage_run_id="stage-test",
                    batch_id=evaluation_batch_id,
                )
            )

        compared = ModelComparisonService(
            self.repository, self.benchmark, self.pack, TEST_SCHEMA
        ).compare(
            baseline_model_version="x2.0",
            candidate_model_version="x2.1",
            baseline_run_batch_id="batch-x2.0",
            candidate_run_batch_id="batch-x2.1",
            baseline_evaluation_batch_id="eval-batch-basis-x2.0",
            candidate_evaluation_batch_id="eval-batch-basis-x2.1",
            requested_scene_ids=["core-indoor-selfie-person-replacement"],
        )

        self.assertEqual(compared["status"], "not_comparable")
        self.assertEqual(compared["pairs"], [])
        self.assertIn(
            "score_basis",
            {item["kind"] for item in compared["comparability"]["differences"]},
        )


class ClassificationTests(ReportingTestBase):
    def test_classify_delta_by_schema_thresholds(self) -> None:
        from xmax_test.reporting.classification import classify_delta

        policy = TEST_SCHEMA["comparison_policy"]
        self.assertEqual(classify_delta(6.0, policy), "p0")
        self.assertEqual(classify_delta(1.0, policy), "p1")
        self.assertEqual(classify_delta(-6.0, policy), "p2")
        self.assertEqual(classify_delta(-1.0, policy), "p1")
        # No policy -> unclassified, never invented.
        self.assertEqual(classify_delta(50.0, None), "unclassified")

    def test_new_hard_gate_failure_is_always_p2(self) -> None:
        baseline = {
            "_evaluation": {
                "applied_gate_ids": [],
                "final_verdict": None,
                "canonical_score": 90.0,
                "scenario_score": 90.0,
            }
        }
        candidate = {
            "_evaluation": {
                "applied_gate_ids": ["invalid-result-block-score"],
                "final_verdict": "invalid_result",
                "canonical_score": 95.0,
                "scenario_score": 95.0,
            }
        }
        pair = {
            "key": ("feed001_prompt001_01", "core-indoor-selfie-person-replacement", "offline"),
            "scene_id": "core-indoor-selfie-person-replacement",
            "mode": "offline",
            "baseline": baseline,
            "candidate": candidate,
        }
        classified = classify_pair(pair, TEST_SCHEMA["comparison_policy"])
        self.assertEqual(classified["classification"], "p2")
        self.assertTrue(classified["new_hard_gate_failure"])

    def test_bucket_pairs(self) -> None:
        policy = TEST_SCHEMA["comparison_policy"]
        pairs = [
            {
                "key": ("a", "s1", "offline"),
                "scene_id": "s1",
                "mode": "offline",
                "baseline": {
                    "_evaluation": {
                        "canonical_score": 50.0,
                        "scenario_score": 50.0,
                        "applied_gate_ids": [],
                        "final_verdict": None,
                    }
                },
                "candidate": {
                    "_evaluation": {
                        "canonical_score": 60.0,
                        "scenario_score": 60.0,
                        "applied_gate_ids": [],
                        "final_verdict": None,
                    }
                },
            },
            {
                "key": ("b", "s1", "offline"),
                "scene_id": "s1",
                "mode": "offline",
                "baseline": {
                    "_evaluation": {
                        "canonical_score": 60.0,
                        "scenario_score": 60.0,
                        "applied_gate_ids": [],
                        "final_verdict": None,
                    }
                },
                "candidate": {
                    "_evaluation": {
                        "canonical_score": 50.0,
                        "scenario_score": 50.0,
                        "applied_gate_ids": [],
                        "final_verdict": None,
                    }
                },
            },
        ]
        buckets = bucket_pairs(pairs, policy)
        self.assertEqual(len(buckets["p0"]), 1)
        self.assertEqual(len(buckets["p2"]), 1)


class RenderTests(ReportingTestBase):
    def test_repeat_stability_uses_group_population_standard_deviation(self) -> None:
        repeat_summary = {
            "groups": [
                {
                    "group_id": "repeat-stable",
                    "configured_repeat_count": 3,
                    "member_run_ids": ["run-1", "run-2", "run-3"],
                    "mode": "offline",
                    "scenario_id": "scene-a",
                    "case_score_percent": {
                        "count": 3,
                        "mean": 80.0,
                        "minimum": 70.0,
                        "maximum": 90.0,
                        "standard_deviation": 8.165,
                    },
                    "case_score_spread": 20.0,
                },
                {
                    "group_id": "repeat-unstable",
                    "configured_repeat_count": 3,
                    "member_run_ids": ["run-4", "run-5", "run-6"],
                    "mode": "offline",
                    "scenario_id": "scene-a",
                    "case_score_percent": {
                        "count": 3,
                        "mean": 60.0,
                        "minimum": 45.0,
                        "maximum": 75.0,
                        "standard_deviation": 12.247,
                    },
                    "case_score_spread": 30.0,
                },
            ]
        }
        cases = [
            {
                "run_id": f"run-{index}",
                "case_number": f"case-{index}",
                "score_basis_signature": "same-basis",
            }
            for index in range(1, 7)
        ]

        result = _repeat_stability_diagnostics(
            repeat_summary,
            cases,
            standard_deviation_threshold_points=10.0,
        )

        self.assertEqual(result["stable_group_count"], 1)
        self.assertEqual(result["unstable_group_count"], 1)
        self.assertEqual(result["unstable_group_rate_percent"], 50.0)
        self.assertEqual(result["group_standard_deviation"]["mean"], 10.21)
        self.assertNotIn("between_group", result)

    def test_repeat_stability_excludes_groups_with_mixed_score_bases(self) -> None:
        repeat_summary = {
            "groups": [
                {
                    "group_id": "repeat-mixed-basis",
                    "configured_repeat_count": 2,
                    "member_run_ids": ["run-1", "run-2"],
                    "mode": "offline",
                    "scenario_id": "scene-a",
                    "case_score_percent": {
                        "count": 2,
                        "mean": 70.0,
                        "minimum": 50.0,
                        "maximum": 90.0,
                        "standard_deviation": 20.0,
                    },
                    "case_score_spread": 40.0,
                }
            ]
        }
        cases = [
            {
                "run_id": "run-1",
                "case_number": "case-1",
                "score_basis_signature": "basis-a",
            },
            {
                "run_id": "run-2",
                "case_number": "case-2",
                "score_basis_signature": "basis-b",
            },
        ]

        result = _repeat_stability_diagnostics(
            repeat_summary,
            cases,
            standard_deviation_threshold_points=10.0,
        )

        self.assertEqual(result["classified_group_count"], 0)
        self.assertEqual(result["basis_mismatch_group_count"], 1)
        self.assertEqual(
            result["unclassified_groups"][0]["classification"], "basis_mismatch"
        )

    def test_legacy_group_criteria_do_not_affect_new_report_credibility(self) -> None:
        cases = [
            {
                "run_id": "run-a",
                "status": "completed",
                "mode": "offline",
                "feed_asset_id": "feed-a",
                "prompt_asset_ids": [],
                "prompt_text": "动作A",
                "operation_recipe_id": "recipe",
                "scenario_id": "scene",
            },
            {
                "run_id": "run-b",
                "status": "completed",
                "mode": "offline",
                "feed_asset_id": "feed-b",
                "prompt_asset_ids": [],
                "prompt_text": "动作B",
                "operation_recipe_id": "recipe",
                "scenario_id": "scene",
            },
        ]
        evaluations = {
            "run-a": {
                "run_id": "run-a",
                "criterion_results": [
                    {
                        "criterion_id": "O4.1",
                        "raw_metrics": [
                            {
                                "values": {
                                    "attempt_count": 2,
                                    "member_run_ids": ["run-a", "run-b"],
                                }
                            }
                        ],
                    }
                ],
            },
            "run-b": {"run_id": "run-b", "criterion_results": []},
        }

        result = _credibility(cases, evaluations, run_count=2)

        self.assertEqual(result["status"], "credible_with_warnings")
        self.assertNotIn(
            "group_scope_membership_mismatch",
            {item["code"] for item in result["issues"]},
        )

    def test_single_version_report_uses_exact_batches_and_has_criteria_section(
        self,
    ) -> None:
        result = SingleVersionReportService(self.repository, self.benchmark, self.pack).generate(
            report_id="single-x2.0",
            model_version="x2.0",
            run_batch_id="batch-x2.0",
            evaluation_batch_id="eval-batch-x2.0",
            output_directory=self.output,
            requested_scene_ids=["core-indoor-selfie-person-replacement"],
        )
        payload = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
        schema = json.loads(
            (ROOT / "schemas" / "single-version-report.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(payload)
        markdown = Path(result["markdown_path"]).read_text(encoding="utf-8")
        self.assertIn("## 细则结果", markdown)
        self.assertNotIn("## 评价要求全量覆盖", markdown)
        self.assertNotIn("### 0/1/2评分细则", markdown)
        self.assertIn("## 批次与运行指标", markdown)
        self.assertNotIn("## 可信度检查", markdown)
        self.assertNotIn("## 模型与实时运行性能", markdown)
        self.assertIn("| 维度 | 细则 | 状态 | 得分 | 可评/应出现 |", markdown)
        self.assertEqual(markdown.count("| P 模型性能与基础可用性 | P.1 单次结果有效性 |"), 1)
        criteria_section = markdown.split("## 细则结果", 1)[1].split(
            "## P0 证据包（待Agent分析）", 1
        )[0]
        benchmark_criteria = [
            criterion
            for dimension in self.benchmark["dimensions"]
            for criterion in dimension.get("criteria", [])
        ]
        self.assertEqual(criteria_section.count("\n| ") - 2, len(benchmark_criteria))
        for criterion in benchmark_criteria:
            self.assertEqual(
                criteria_section.count(
                    f"{criterion['criterion_id']} {criterion.get('name', '')}"
                ),
                1,
            )
        self.assertIn("## P.3 同输入重复稳定性", markdown)
        self.assertIn("组内总分标准差", markdown)
        self.assertIn("标准差（百分点）", markdown)
        self.assertNotIn("组均分两两比较", markdown)
        self.assertNotIn("| 不稳定重复组 |", markdown)
        self.assertIn("组内总分标准差", markdown)
        self.assertIn('rowspan="', markdown)
        self.assertIn("## P0 证据包（待Agent分析）", markdown)
        self.assertTrue(payload["analysis_required"])
        stability = payload["reporting_metrics"]["P.3"]["diagnostic_stability"]
        self.assertEqual(
            stability["policy_id"],
            "repeat-score-population-sd-10-comparable-basis-v2",
        )
        self.assertNotIn("between_group", stability)
        self.assertIn("repeat_group_standard_deviation", payload["case_results"][0])
        self.assertIn("repeat_group_stability", payload["case_results"][0])
        self.assertEqual(payload["report_schema_version"], "single-version-report/1.2")
        requirements = payload["evaluation_requirement_results"]
        benchmark_requirement_count = sum(
            len(item.get("criteria", [])) for item in self.benchmark["dimensions"]
        ) + len(self.benchmark["reporting_metrics"])
        self.assertEqual(requirements["requirement_count"], benchmark_requirement_count)
        self.assertEqual(
            {item["requirement_id"] for item in requirements["reporting_metrics"]},
            {"P.2", "P.3", "RP.1", "RP.2"},
        )
        self.assertNotIn("Recommendation:", markdown)

    def test_full_report_writes_json_and_markdown_without_placeholders(self) -> None:
        result = self.service().generate(
            comparison_id="cmp-1",
            baseline_model_version="x2.0",
            candidate_model_version="x2.1",
            **self.selectors(),
            requested_scene_ids=["core-indoor-selfie-person-replacement"],
            template_path=self.template,
            output_directory=self.output,
        )
        md_path = Path(result["markdown_path"])
        json_path = Path(result["json_path"])
        self.assertTrue(md_path.is_file())
        self.assertTrue(json_path.is_file())
        md_text = md_path.read_text(encoding="utf-8")
        self.assertNotIn("{{", md_text)
        self.assertIn("### P0 — 新模型明显改进", md_text)
        self.assertIn("### P1 — 新模型持平", md_text)
        self.assertIn("### P2 — 新模型劣化", md_text)
        self.assertIn("## 4. 全量维度变化", md_text)
        self.assertIn("## 5. 全量细则变化", md_text)
        self.assertIn("P.3 同输入重复稳定性", md_text)
        self.assertIn("基线/新版标准差（百分点）", md_text)
        self.assertNotIn("Canonical Score", md_text)
        self.assertNotIn("complete / partial / not_comparable", md_text)
        self.assertNotIn("promote / shadow / block", md_text)
        self.assertIn("3.1", md_text)

        report = json.loads(json_path.read_text(encoding="utf-8"))
        schema = json.loads(
            (ROOT / "schemas" / "model-version-report.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(report)
        self.assertIn("p0_improvements", report)
        self.assertTrue(report["analysis_required"])
        self.assertEqual(
            len(report["dimension_results"]),
            sum(
                1
                for dimension in self.benchmark["dimensions"]
                if "offline" in dimension.get("applicable_modes", [])
                or "both" in dimension.get("applicable_modes", [])
            ),
        )
        self.assertEqual(
            len(report["criterion_results"]),
            sum(len(dimension.get("criteria", [])) for dimension in self.benchmark["dimensions"]),
        )
        self.assertTrue(
            {"P.2", "P.3", "RP.1", "RP.2"}.issubset(
                report["reporting_metric_results"]["baseline"]
            )
        )
        self.assertTrue(report["generation_config_hash"])
        self.assertIn("template_hash", report["audit"])
        self.assertEqual(
            report["audit"]["template_hash"],
            template_hash(self.template.read_text(encoding="utf-8")),
        )

    def test_each_requested_scene_has_own_section(self) -> None:
        result = self.service().generate(
            comparison_id="cmp-2",
            baseline_model_version="x2.0",
            candidate_model_version="x2.1",
            **self.selectors(),
            requested_scene_ids=["core-indoor-selfie-person-replacement"],
            template_path=self.template,
            output_directory=self.output,
        )
        md_text = Path(result["markdown_path"]).read_text(encoding="utf-8")
        self.assertIn("### 3.1", md_text)

    def test_unfilled_template_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            render_markdown({"no": "data"}, "still {{ unreplaced }} here")

    def test_report_service_rejects_missing_template(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.service().generate(
                comparison_id="cmp-3",
                baseline_model_version="x2.0",
                candidate_model_version="x2.1",
                **self.selectors(),
                requested_scene_ids=["core-indoor-selfie-person-replacement"],
                template_path=self.output / "missing.md",
                output_directory=self.output,
            )


if __name__ == "__main__":
    unittest.main()
