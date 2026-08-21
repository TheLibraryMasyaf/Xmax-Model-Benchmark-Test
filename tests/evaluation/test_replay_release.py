"""P11 Release/Replay tests.

Acceptance: Holdout isolation; old results never overwritten; new/old
difference report; failed version cannot be promoted; Champion can rollback.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xmax_test.benchmark import load_benchmark_contract
from xmax_test.errors import ContractError
from xmax_test.evaluation.orchestrator import EvaluationOrchestrator
from xmax_test.evaluation.preprocess import PreprocessService
from xmax_test.evaluation.release import ReleaseService
from xmax_test.evaluation.replay import ReplayService
from xmax_test.judges.registry import JudgeRegistry
from xmax_test.judges.releases import JudgeReleaseService
from xmax_test.judges.worker import JudgeWorker
from xmax_test.scenarios import load_scenario_pack
from xmax_test.storage.artifacts import ArtifactStore
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.time import FixedClock

ROOT = Path(__file__).resolve().parents[2]


class MetricJudge:
    """A metric judge for C1/O5-style dimensions; the score is mutable."""

    def __init__(self, dimension: str, score: float) -> None:
        self._dimension = dimension
        self._score = score

    def set_score(self, score: float) -> None:
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
        criterion_results = [
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
                "criterion_results": criterion_results,
            }
        ]


class ReleaseReplayTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3", clock=FixedClock())
        self.artifacts = ArtifactStore(root / "artifacts")
        self.benchmark = load_benchmark_contract(ROOT / "BENCHMARK.md")
        self.pack = load_scenario_pack(ROOT / "config" / "scenarios.json")
        self.clock = FixedClock()
        self.registry = JudgeRegistry()
        self.worker = JudgeWorker(self.registry, self.artifacts)
        self.preprocess = PreprocessService(self.repository, self.artifacts)
        self.score_schema = next(
            s for s in self.benchmark.get("score_schemas", []) if s.get("status") == "shadow"
        )

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
            clock=self.clock,
        )


class ReplayTests(ReleaseReplayTestBase):
    def test_old_results_never_overwritten(self) -> None:
        self.registry.register(MetricJudge("C1", 2.0))
        run = self.seed_run()
        first = self.orchestrator().evaluate_run(run, "batch-first")
        old_id = first["evaluation_id"]
        self.assertIsNotNone(self.repository.get_evaluation_result(old_id))
        # Replay re-evaluates and must NOT overwrite the old evaluation.
        replay = ReplayService(
            self.repository, self.orchestrator(), self.artifacts, clock=self.clock
        )
        outcome = replay.replay_runs([run])
        self.assertEqual(outcome["old_results_preserved"], True)
        self.assertTrue(
            any(
                r.get("evaluation_id") != old_id
                for r in self.repository.list_evaluation_results(run_id=run["run_id"])
            )
        )
        # The old evaluation is still readable under its original ID.
        self.assertIsNotNone(self.repository.get_evaluation_result(old_id))

    def test_replay_reports_new_old_differences(self) -> None:
        run = self.seed_run()
        # First evaluation with C1=2.0.
        judge = MetricJudge("C1", 2.0)
        self.registry.register(judge)
        first = self.orchestrator().evaluate_run(run, "batch-first")
        self.assertEqual(first["evaluation_id"] is not None, True)
        # Replay with a different score producing a different result.
        judge.set_score(0.0)
        replay = ReplayService(
            self.repository, self.orchestrator(), self.artifacts, clock=self.clock
        )
        outcome = replay.replay_runs([run])
        self.assertTrue(outcome["differences"])
        diff = outcome["differences"][0]
        self.assertEqual(diff["run_id"], run["run_id"])
        self.assertNotEqual(diff["new_evaluation_id"], diff["old_evaluation_id"])
        self.assertIsNone(diff["delta_canonical"])
        self.assertTrue(diff["changed_dimensions"])


class ReleaseTests(ReleaseReplayTestBase):
    def test_validation_requires_holdout_evidence(self) -> None:
        service = ReleaseService(self.repository, self.benchmark, self.score_schema)
        with self.assertRaises(ContractError):
            service.validate(holdout_results=[], threshold=0.5)

    def test_failed_version_cannot_be_promoted(self) -> None:
        service = ReleaseService(self.repository, self.benchmark, self.score_schema)
        validation = service.validate(
            holdout_results=[{"applied_gate_ids": ["hard-gate-1"], "final_verdict": "fail"}],
            threshold=0.5,
        )
        self.assertFalse(validation["valid"])
        with self.assertRaises(ContractError):
            service.promote(validation, operator="tester")

    def test_valid_holdout_can_be_promoted(self) -> None:
        service = ReleaseService(self.repository, self.benchmark, self.score_schema)
        validation = service.validate(
            holdout_results=[{"applied_gate_ids": [], "final_verdict": "pass"}],
            threshold=0.5,
        )
        self.assertTrue(validation["valid"])
        outcome = service.promote(validation, operator="tester")
        self.assertEqual(outcome["status"], "promoted")

    def test_champion_can_rollback(self) -> None:
        service = ReleaseService(self.repository, self.benchmark, self.score_schema)
        validation = service.validate(
            holdout_results=[{"applied_gate_ids": [], "final_verdict": "pass"}],
            threshold=0.5,
        )
        service.promote(validation, operator="tester")
        outcome = service.rollback("0.1.0-draft", operator="tester")
        self.assertEqual(outcome["status"], "rolled_back")

    def test_judge_release_lifecycle(self) -> None:
        service = JudgeReleaseService(self.repository, clock=self.clock)
        # Shadow first, then promote after valid validation.
        self.repository.record_judge_release(
            "video-quality",
            "2.0.0",
            "shadow",
            {"judge_id": "video-quality", "version": "2.0.0", "status": "shadow"},
        )
        service.promote(
            "video-quality",
            "2.0.0",
            validation={"valid": True, "data_partition": "holdout"},
        )
        self.assertEqual(service.state("video-quality", "2.0.0")["action"], "promote")
        # Failed validation cannot promote.
        with self.assertRaises(ContractError):
            service.promote("video-quality", "2.0.0", validation={"valid": False})
        # Champion rollback.
        outcome = service.rollback("video-quality", "1.0.0", reason="regression")
        self.assertEqual(outcome["status"], "champion")


if __name__ == "__main__":
    unittest.main()
