"""P8 + P1 Evaluation tests: orchestrator, fusion, hard gates."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from xmax_test.benchmark import load_benchmark_contract
from xmax_test.errors import ContractError, EvaluationInfrastructurePausedError
from xmax_test.evaluation.aggregation import aggregate_evaluation_results
from xmax_test.evaluation.fusion import JudgmentFusion
from xmax_test.evaluation.gates import HardGateEvaluator
from xmax_test.evaluation.orchestrator import EvaluationOrchestrator
from xmax_test.evaluation.preprocess import PreprocessService
from xmax_test.judges.registry import JudgeRegistry
from xmax_test.judges.worker import JudgeWorker
from xmax_test.planning.recipes import RecipeResolver
from xmax_test.scenarios import load_scenario_pack
from xmax_test.storage.artifacts import ArtifactStore
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.time import FixedClock

ROOT = Path(__file__).resolve().parents[2]


class MetricJudge:
    """A metric judge for C1/O5-style dimensions."""

    def __init__(self, dimension: str, score: float) -> None:
        self._dimension = dimension
        self._score = score

    def manifest(self):
        return {
            "judge_id": f"metric-{self._dimension}",
            "version": "1.0.0",
            "kind": "metric",
            "supported_dimensions": [self._dimension],
            "supported_modes": ["offline", "realtime"],
        }

    def evaluate(self, context):
        criteria = [
            {
                "criterion_id": item["criterion_id"],
                "verdict": "ok",
                "score": self._score,
                "confidence": 0.9,
                "assessable": True,
                "evidence": [{"description": "metric fact"}],
            }
            for item in context.get("dimension_contract", {}).get("criteria", [])
        ]
        return [
            {
                "dimension_id": self._dimension,
                "verdict": "ok",
                "score": self._score,
                "confidence": 0.9,
                "assessable": True,
                "evidence": [{"description": "metric fact"}],
                "criterion_results": criteria,
            }
        ]


class EvaluationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3", clock=FixedClock())
        self.artifacts = ArtifactStore(root / "artifacts")
        self.benchmark = load_benchmark_contract(ROOT / "BENCHMARK.md")
        self.pack = load_scenario_pack(ROOT / "config" / "scenarios.json")
        self.registry = JudgeRegistry()
        self.worker = JudgeWorker(self.registry, self.artifacts)
        self.preprocess = PreprocessService(self.repository, self.artifacts)
        self.clock = FixedClock()

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def seed_run(self, *, mode: str = "offline", case_id: str = "case-1") -> dict:
        self.repository.upsert_asset(
            {
                "asset_id": "result-asset",
                "kind": "result_video",
                "uri": "artifact://assets/result-asset/source.bin",
                "sha256": "sha-result",
                "bytes": 10,
                "status": "ready",
                "media": {
                    "duration_s": 8.0,
                    "width": 704,
                    "height": 1280,
                    "fps": 24.0,
                    "has_audio": True,
                },
            }
        )
        self.repository.upsert_asset(
            {
                "asset_id": "feed-a",
                "kind": "feed_video",
                "uri": "artifact://assets/feed-a/source.bin",
                "sha256": "sha-feed",
                "bytes": 10,
                "status": "ready",
                "media": {"duration_s": 8.0, "width": 704, "height": 1280, "fps": 24.0},
            }
        )
        self.repository.upsert_test_case(
            {
                "case_id": case_id,
                "case_number": "feed001_prompt001_01",
                "feed_number": "feed001",
                "prompt_number": "prompt001",
                "feed_asset_id": "feed-a",
                "prompt_asset_ids": [],
                "prompt_text": "换装",
                "generation_mode": mode,
                "repeat_index": 1,
                "model_id": "x2.0",
                "operation_recipe_id": "offline-image-reference",
                "operation_recipe_version": "0.1.0",
                "edited_video_asset_id": "feed-a",
                "expected_audio_source_asset_id": "feed-a",
                "scenario_id": "core-selfie-appearance",
                "scenario_pack_version": self.pack.get("version"),
                "scene_tags": {
                    "input_dimension": "自拍",
                    "instruction_dimension": "修改主体",
                },
            },
            "plan-1",
        )
        run_id = f"run-{case_id}"
        self.repository.create_run(
            {
                "run_id": run_id,
                "run_batch_id": "batch-1",
                "case_id": case_id,
                "case_number": "feed001_prompt001_01",
                "status": "completed",
                "model_id": "x2.0",
                "mode": mode,
                "origin": "xmax_offline",
                "provenance": {
                    "source_type": "t",
                    "source_locator": "l",
                    "source_hash": "h",
                },
                "result_asset_id": "result-asset",
                "edited_video_asset_id": "feed-a",
                "expected_audio_source_asset_id": "feed-a",
                "metrics": {"credits": 100, "failure_class": None},
            }
        )
        run = self.repository.get_run(run_id)
        self.preprocess.build(run)
        return run

    def orchestrator(self) -> EvaluationOrchestrator:
        return EvaluationOrchestrator(
            self.repository,
            self.artifacts,
            self.benchmark,
            self.pack,
            self.registry,
            self.worker,
            self.preprocess,
            recipe_resolver=RecipeResolver(ROOT / "config" / "operation-recipes.json"),
            clock=self.clock,
        )


class FusionTests(EvaluationTestBase):
    def test_dimension_score_is_derived_from_criteria_not_judge_top_level(self) -> None:
        judgments = [
            {
                "dimension_id": "C1",
                "judge_id": "metric",
                "judge_version": "1",
                "verdict": "misleading-top-level",
                "score": 0.0,
                "confidence": 0.9,
                "assessable": True,
                "evidence": [],
                "criterion_results": [
                    {
                        "criterion_id": "C1.1",
                        "verdict": "good",
                        "score": 2.0,
                        "confidence": 0.9,
                        "assessable": True,
                        "evidence": [],
                    },
                    {
                        "criterion_id": "C1.2",
                        "verdict": "bad",
                        "score": 0.0,
                        "confidence": 0.9,
                        "assessable": True,
                        "evidence": [],
                    },
                ],
            }
        ]
        result = JudgmentFusion().fuse(
            self.benchmark,
            self.pack,
            {"generation_mode": "offline", "scenario_id": None, "scene_tags": {}},
            judgments,
            {},
        )
        c1 = next(item for item in result["dimension_results"] if item["dimension_id"] == "C1")
        self.assertEqual(c1["score"], 1.0)
        self.assertEqual(c1["score_percent"], 50.0)
        self.assertEqual(c1["assessable_criterion_count"], 2)

    def test_multi_judge_fusion_happens_within_each_criterion(self) -> None:
        base = {
            "dimension_id": "C9",
            "judge_version": "1",
            "confidence": 0.8,
            "assessable": True,
            "evidence": [],
        }
        judgments = [
            {
                **base,
                "judge_id": "cv",
                "verdict": "cv",
                "criterion_results": [
                    {
                        "criterion_id": "C9.1",
                        "verdict": "good",
                        "score": 2.0,
                        "confidence": 0.8,
                        "assessable": True,
                        "evidence": [],
                    }
                ],
            },
            {
                **base,
                "judge_id": "mlmm",
                "verdict": "mlmm",
                "criterion_results": [
                    {
                        "criterion_id": "C9.1",
                        "verdict": "weak",
                        "score": 0.0,
                        "confidence": 0.8,
                        "assessable": True,
                        "evidence": [],
                    },
                    {
                        "criterion_id": "C9.3",
                        "verdict": "good",
                        "score": 2.0,
                        "confidence": 0.8,
                        "assessable": True,
                        "evidence": [],
                    },
                ],
            },
        ]
        result = JudgmentFusion().fuse(
            self.benchmark,
            self.pack,
            {"generation_mode": "offline", "scenario_id": None, "scene_tags": {}},
            judgments,
            {},
        )
        criteria = {item["criterion_id"]: item for item in result["criterion_results"]}
        self.assertEqual(criteria["C9.1"]["score"], 1.0)
        self.assertEqual(criteria["C9.1"]["judge_score_count"], 2)
        c9 = next(item for item in result["dimension_results"] if item["dimension_id"] == "C9")
        self.assertIsNone(c9["score"])
        self.assertFalse(c9["coverage_complete"])
        self.assertEqual(c9["assessable_criterion_count"], 2)

    def test_batch_criterion_summary_preserves_fractional_scores(self) -> None:
        summary = aggregate_evaluation_results(
            [
                {
                    "case_score_percent": 50.0,
                    "criterion_results": [
                        {
                            "dimension_id": "C1",
                            "criterion_id": "C1.1",
                            "criterion_name": "valid",
                            "score": 0.5,
                        }
                    ],
                    "dimension_results": [],
                },
                {
                    "case_score_percent": 100.0,
                    "criterion_results": [
                        {
                            "dimension_id": "C1",
                            "criterion_id": "C1.1",
                            "criterion_name": "valid",
                            "score": 2.0,
                        }
                    ],
                    "dimension_results": [],
                },
            ]
        )
        row = summary["criterion_summary"][0]
        self.assertEqual(row["score_percent"], 62.5)
        self.assertEqual(row["score_distribution"]["0_to_1"], 1)
        self.assertEqual(row["score_distribution"]["2"], 1)

    def test_canonical_and_scenario_scores_coexist(self) -> None:
        judgments = [
            {
                "evaluation_id": "e",
                "run_id": "r",
                "benchmark_version": "0.1.0-draft",
                "dimension_id": "C1",
                "dimension_version": "0.1.0-draft",
                "judge_id": "metric",
                "judge_version": "1",
                "verdict": "ok",
                "score": 2.0,
                "confidence": 0.9,
                "assessable": True,
                "evidence": [],
                "criterion_results": [
                    {
                        "criterion_id": "C1.1",
                        "verdict": "ok",
                        "score": 2.0,
                        "confidence": 0.9,
                        "assessable": True,
                        "evidence": [],
                    }
                ],
            },
            {
                "evaluation_id": "e",
                "run_id": "r",
                "benchmark_version": "0.1.0-draft",
                "dimension_id": "C2",
                "dimension_version": "0.1.0-draft",
                "judge_id": "metric",
                "judge_version": "1",
                "verdict": "ok",
                "score": 2.0,
                "confidence": 0.9,
                "assessable": True,
                "evidence": [],
                "criterion_results": [
                    {
                        "criterion_id": "C2.1",
                        "verdict": "ok",
                        "score": 2.0,
                        "confidence": 0.9,
                        "assessable": True,
                        "evidence": [],
                    }
                ],
            },
        ]
        result = JudgmentFusion().fuse(
            self.benchmark,
            self.pack,
            {
                "case_id": "case-1",
                "generation_mode": "offline",
                "scenario_id": "core-selfie-appearance",
                "scene_tags": {"input_dimension": "自拍"},
            },
            judgments,
            {},
        )
        self.assertIsNone(result["canonical_score"])
        self.assertIsNone(result["scenario_score"])
        self.assertIsNone(result["case_score_percent"])
        self.assertTrue(result["coverage"]["canonical_missing_dimensions"])
        self.assertEqual(result["weight_resolution"]["base_profile_id"], "generic-offline-0.2")
        self.assertTrue(result["weight_resolution"]["matched_rule_ids"])

    def test_hard_gate_blocks_score_regardless_of_weights(self) -> None:
        judgments = [
            {
                "evaluation_id": "e",
                "run_id": "r",
                "benchmark_version": "0.1.0-draft",
                "dimension_id": "C1",
                "dimension_version": "0.1.0-draft",
                "judge_id": "metric",
                "judge_version": "1",
                "verdict": "invalid",
                "score": 0.0,
                "confidence": 1.0,
                "assessable": True,
                "evidence": [{"description": "black screen"}],
                "criterion_results": [
                    {
                        "criterion_id": "C1.1",
                        "verdict": "invalid",
                        "score": 0.0,
                        "confidence": 1.0,
                        "assessable": True,
                        "evidence": [{"description": "black screen"}],
                    }
                ],
            }
        ]
        result = JudgmentFusion().fuse(
            self.benchmark,
            self.pack,
            {
                "case_id": "c",
                "generation_mode": "offline",
                "scenario_id": None,
                "scene_tags": {},
            },
            judgments,
            {},
        )
        self.assertEqual(result["case_score_percent"], 0.0)
        self.assertIn("invalid-result-block-score", result["applied_gate_ids"])
        self.assertEqual(result["final_verdict"], "invalid_result")

    def test_hard_gate_evaluator(self) -> None:
        evaluator = HardGateEvaluator()
        outcome = evaluator.evaluate(
            self.benchmark,
            {"C1": {"score": 0.0}},
            {},
            {"C1.1": {"dimension_id": "C1", "score": 0.0}},
        )
        self.assertTrue(outcome["block_score"])
        self.assertEqual(outcome["final_verdict"], "invalid_result")
        outcome_ok = evaluator.evaluate(
            self.benchmark,
            {"C1": {"score": 2.0}},
            {},
            {"C1.1": {"dimension_id": "C1", "score": 2.0}},
        )
        self.assertFalse(outcome_ok["block_score"])

    def test_weight_resolution_saves_hit_rules(self) -> None:
        judgments = [
            {
                "evaluation_id": "e",
                "run_id": "r",
                "benchmark_version": "0.1.0-draft",
                "dimension_id": "C1",
                "dimension_version": "0.1.0-draft",
                "judge_id": "metric",
                "judge_version": "1",
                "verdict": "ok",
                "score": 1.0,
                "confidence": 0.8,
                "assessable": True,
                "evidence": [],
                "criterion_results": [
                    {
                        "criterion_id": "C1.1",
                        "verdict": "ok",
                        "score": 1.0,
                        "confidence": 0.8,
                        "assessable": True,
                        "evidence": [],
                    }
                ],
            }
        ]
        result = JudgmentFusion().fuse(
            self.benchmark,
            self.pack,
            {
                "case_id": "c",
                "generation_mode": "offline",
                "scenario_id": "core-selfie-appearance",
                "scene_tags": {"input_dimension": "自拍"},
            },
            judgments,
            {},
        )
        resolution = result["weight_resolution"]
        self.assertIn("core-selfie-appearance-offline", resolution["matched_rule_ids"])
        self.assertGreater(sum(resolution["effective_weights"].values()), 0.99)
        self.assertLessEqual(sum(resolution["effective_weights"].values()), 1.01)


class OrchestratorTests(EvaluationTestBase):
    def test_preprocess_recovers_missing_webm_duration(self) -> None:
        class RecoveredMediaValidator:
            def validate(self, path, kind):
                self.path = path
                self.kind = kind
                return {
                    "duration_s": 8.597,
                    "duration_source": "packet_timeline",
                    "fps": 16.053,
                    "width": 832,
                    "height": 1504,
                    "video_codec": "vp8",
                    "has_audio": False,
                }

        run = self.seed_run(mode="realtime")
        self.artifacts.put_bytes("assets", "result-asset/source.bin", b"webm-packets")
        asset = self.repository.get_asset("result-asset")
        self.repository.upsert_asset({**asset, "media": {"duration_s": None}})
        validator = RecoveredMediaValidator()
        service = PreprocessService(
            self.repository,
            self.artifacts,
            media_validator=validator,
        )

        preprocess = service.build(run)

        self.assertEqual(preprocess["global_timestamps"][-1], 8.564)
        recovered = self.repository.get_asset("result-asset")["media"]
        self.assertEqual(recovered["duration_s"], 8.597)
        self.assertEqual(recovered["duration_source"], "packet_timeline")
        self.assertEqual(validator.kind, "result_video")

    def test_direct_media_inputs_keep_feed_prompt_reference_and_result_separate(
        self,
    ) -> None:
        run = self.seed_run()
        self.artifacts.put_bytes("assets", "feed-a/source.bin", b"\x00\x00\x00\x18ftypisom-feed")
        self.artifacts.put_bytes(
            "assets", "result-asset/source.bin", b"\x00\x00\x00\x18ftypisom-result"
        )
        prompt_stored = self.artifacts.put_bytes(
            "assets", "prompt-a/source.bin", b"\x89PNG\r\n\x1a\n-prompt"
        )
        self.repository.upsert_asset(
            {
                "asset_id": "prompt-a",
                "kind": "prompt_image",
                "uri": prompt_stored["uri"],
                "sha256": "sha-prompt",
                "bytes": prompt_stored["bytes"],
                "status": "ready",
                "media": {"width": 512, "height": 512},
            }
        )
        case = self.repository.get_test_case(run["case_id"])
        case["prompt_asset_ids"] = ["prompt-a"]
        self.repository.upsert_test_case(case, "plan-1")

        orchestrator = self.orchestrator()
        operation = orchestrator._operation_contract(case, run["mode"])
        media = orchestrator._media_inputs(case, run, operation)

        self.assertEqual(
            [item["role"] for item in media],
            [
                "generation_operation",
                "feed",
                "prompt_text",
                "prompt_reference_1",
                "result_video",
            ],
        )
        self.assertEqual(
            [item["kind"] for item in media],
            ["text", "video", "text", "image", "video"],
        )

    def test_video_reference_operation_explains_feed_capture_replaces_prompt_subject(
        self,
    ) -> None:
        case = {
            "operation_recipe_id": "offline-video-reference-with-feed-capture",
            "operation_recipe_version": "0.1.0",
            "feed_asset_id": "feed-a",
            "prompt_asset_ids": ["prompt-video"],
            "edited_video_asset_id": "prompt-video",
            "expected_audio_source_asset_id": "prompt-video",
            "api_asset_bindings": {
                "refVideoPath": "prompt_video",
                "refImagePath": "feed_capture",
            },
        }
        contract = self.orchestrator()._operation_contract(case, "offline")
        self.assertEqual(contract["edited_video_role"], "prompt_video")
        self.assertEqual(contract["api_asset_bindings"]["refImagePath"], "feed_capture")
        self.assertIn("Feed截图", contract["result_expectation"])
        self.assertIn("Prompt视频", contract["result_expectation"])

    def test_video_reference_sends_actual_feed_capture_as_separate_input(self) -> None:
        run = self.seed_run()
        self.artifacts.put_bytes(
            "captures",
            "feed-a-sha-feed/middle.jpg",
            b"\xff\xd8\xff-feed-capture",
        )
        case = self.repository.get_test_case(run["case_id"])
        case.update(
            {
                "operation_recipe_id": "offline-video-reference-with-feed-capture",
                "api_asset_bindings": {
                    "refVideoPath": "prompt_video",
                    "refImagePath": "feed_capture",
                },
            }
        )
        orchestrator = self.orchestrator()
        media = orchestrator._media_inputs(
            case, run, orchestrator._operation_contract(case, "offline")
        )
        capture = next(item for item in media if item["role"] == "feed_capture")
        self.assertEqual(capture["kind"], "image")
        self.assertTrue(capture["path"].endswith("feed-a-sha-feed/middle.jpg"))

    def test_direct_media_reuses_xmax_urls_by_result_and_asset_hash(self) -> None:
        run = self.seed_run()
        result_url = "https://media.example.test/generated.mp4"
        feed_url = "https://media.example.test/sha-feed.mp4"
        run = {**run, "metrics": {**run.get("metrics", {}), "result_url": result_url}}
        self.repository.append_event(
            run["run_id"],
            "status_submitted",
            payload={"state": {"refVideoPath": feed_url}},
        )
        urls = self.orchestrator()._run_media_urls(
            self.repository.get_test_case(run["case_id"]), run
        )
        self.assertEqual(urls["feed-a"], feed_url)
        self.assertEqual(urls["result-asset"], result_url)

    def test_preprocess_groups_feed_and_result_evidence(self) -> None:
        run = self.seed_run()
        preprocess = self.repository.get_preprocess_for_run(run["run_id"])
        roles = [item["role"] for item in preprocess["evidence_groups"]]
        self.assertEqual(roles, ["feed", "result_video"])

    def test_evaluate_run_produces_versioned_result(self) -> None:
        self.registry.register(MetricJudge("C1", 2.0))
        self.registry.register(MetricJudge("C2", 2.0))
        run = self.seed_run()
        result = self.orchestrator().evaluate_run(run, "batch-eval")
        self.assertEqual(result["run_id"], run["run_id"])
        self.assertEqual(result["benchmark_version"], "0.2.0-draft")
        self.assertIn("canonical_score", result)
        self.assertIn("scenario_score", result)
        self.assertIsNone(result["case_score_percent"])
        self.assertTrue(result["coverage"]["canonical_missing_dimensions"])

    def test_evaluate_batch_persists_batch_manifest(self) -> None:
        self.registry.register(MetricJudge("C1", 2.0))
        run = self.seed_run()
        summary = self.orchestrator().evaluate_runs([run])
        self.assertEqual(summary["evaluated"], 1)
        manifest = self.repository.get_batch_manifest(
            "evaluation_batch", summary["evaluation_batch_id"]
        )
        self.assertEqual(manifest["entity_type"], "evaluation_batch")
        self.assertTrue(manifest["metadata"]["aggregate"]["criterion_summary"])

    def test_infrastructure_pause_stops_batch_without_per_run_errors(self) -> None:
        runs = [self.seed_run(case_id="case-a"), self.seed_run(case_id="case-b")]
        orchestrator = self.orchestrator()
        calls = []

        def pause(run, evaluation_batch_id, *, preprocess=None):
            calls.append(run["run_id"])
            raise EvaluationInfrastructurePausedError("provider unavailable")

        orchestrator.evaluate_run = pause
        with self.assertRaises(EvaluationInfrastructurePausedError):
            orchestrator.evaluate_runs(runs)
        self.assertEqual(calls, ["run-case-a"])

    def test_evaluation_result_passes_schema(self) -> None:
        self.registry.register(MetricJudge("C1", 1.0))
        run = self.seed_run()
        result = self.orchestrator().evaluate_run(run, "batch-eval")
        schema = json.loads(
            (ROOT / "schemas" / "evaluation-result.schema.json").read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(result)

    def test_unjudgeable_dimensions_are_marked_not_fabricated(self) -> None:
        # No judge registered at all -> C1 etc. become no_automated_judge but
        # the run still produces a result without invented scores.
        run = self.seed_run()
        result = self.orchestrator().evaluate_run(run, "batch-eval")
        self.assertIn("no_automated_judge_dimensions", result)
        self.assertTrue(result["no_automated_judge_dimensions"])

    def test_non_completed_run_is_rejected(self) -> None:
        run = self.seed_run()
        self.repository.update_run_status(run["run_id"], "error")
        run = self.repository.get_run(run["run_id"])
        with self.assertRaises(ContractError):
            self.orchestrator().evaluate_run(run, "batch-eval")

    def test_evaluation_does_not_call_generation_adapter(self) -> None:
        # Composition: the orchestrator only depends on judge registry + worker
        # + preprocess; there is no generation adapter in scope at all.
        self.registry.register(MetricJudge("C1", 2.0))
        run = self.seed_run()
        summary = self.orchestrator().evaluate_runs([run])
        self.assertEqual(summary["evaluated"], 1)


if __name__ == "__main__":
    unittest.main()
