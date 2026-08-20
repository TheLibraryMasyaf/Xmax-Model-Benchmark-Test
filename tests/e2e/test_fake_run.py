"""P13 end-to-end fake-run tests.

Five fully-fake journeys through the Composition root: full pipeline from
assets to report and reconcile; generate-only; old-run evaluate-only; Feishu
Case import then evaluate without writing remote; score-only write-back.
"""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from pathlib import Path

from xmax_test.benchmark import load_benchmark_contract
from xmax_test.cli import Composition, _execute_run_request
from xmax_test.errors import ValidationError
from xmax_test.feishu.client import FakeFeishuSyncClient
from xmax_test.generation.offline.rest_adapter import FakeOfflineTaskTransport
from xmax_test.generation.offline.rtc_adapter import FakeRtcAdapter
from xmax_test.generation.offline.session_api import FakeSessionApiClient
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.tasks import TaskWorker
from xmax_test.tasks.runtime import PipelineTaskRuntime

ROOT = Path(__file__).resolve().parents[2]

FEISHU_JSON = {
    "$schema": "../schemas/feishu-config.schema.json",
    "connection": {"provider": "fake", "identity": "fake"},
    "base": {"url": "https://fake.feishu.cn/base/app", "app_token": "app"},
    "tables": {"feed_data": "tbl-feed", "prompt_data": "tbl-prompt", "case_data": "tbl-case"},
    "field_projection": {
        "case_data": {
            "case_number": "case编号",
            "result_attachment": "case文件",
            "score_percent": "case评分",
            "description": "case说明",
            "feed_attachments": "feed文件",
            "prompt_text": "prompt文字",
            "prompt_attachments": "prompt素材",
            "model_version": "Xmax模型版本",
        }
    },
    "case_score": {
        "source": "scenario_score",
        "display": "percentage",
        "internal_range": [0, 100],
        "feishu_storage_range": [0, 1],
        "write_transform": "divide_by_100",
        "read_transform": "multiply_by_100",
        "unscored_value": None,
        "failed_run_value": 0,
    },
    "write_mode": "upsert",
    "dry_run": False,
}

IMPORT_CONFIG = {
    "import_request_id": "e2e-import",
    "source": {"kind": "feishu_case_data", "base_token": "app", "table_id": "tbl-case"},
    "selector": {"selector_id": "e2e", "state": "request", "entity_type": "source_record", "match": {"filters": {}}},
    "field_mapping": {
        "case_number": "case编号",
        "result_attachment": "case文件",
        "model_version": "Xmax模型版本",
        "feed_attachment": "feed文件",
        "prompt_text": "prompt文字",
        "prompt_attachment": "prompt素材",
        "generation_mode": None,
        "operation_recipe_id": None,
    },
    "mode_resolution": {"order": ["explicit_field", "fixed_default"], "fixed_default": "offline", "on_unresolved": "error"},
    "download": {"result_video": True, "feed_and_prompt_inputs": True},
    "validation": {"media": True, "content_hash": True, "required_case_context": True},
    "imported_run_status": "completed",
    "write_remote": False,
}


class FakeProbe:
    def probe(self, path: Path):
        if path.stat().st_size == 0:
            raise ValidationError("cannot decode")
        return {
            "streams": [{"codec_type": "video", "width": 704, "height": 1280, "avg_frame_rate": "24/1"}],
            "format": {"format_name": "mp4", "duration": "8.0"},
        }


class ScoreBank:
    """Shared mutable score so the same judge can change results."""

    def __init__(self, score: float) -> None:
        self.score = score


class AllDimensionJudge:
    """One judge instance per dimension, reading a shared score."""

    def __init__(self, dimension: str, bank: ScoreBank, kind: str) -> None:
        self._dimension = dimension
        self._bank = bank
        self._kind = kind

    def manifest(self):
        return {
            "judge_id": f"metric-{self._dimension}",
            "version": "1.0.0",
            "kind": self._kind,
            "supported_dimensions": [self._dimension],
            "supported_modes": ["offline", "realtime"],
        }

    def evaluate(self, context):
        return [
            {
                "dimension_id": self._dimension,
                "verdict": "ok",
                "score": self._bank.score,
                "confidence": 0.9,
                "assessable": True,
                "evidence": [{"description": "e2e metric"}],
            }
        ]


class FakeRunE2ETestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "feishu.json").write_text(
            json.dumps(FEISHU_JSON, ensure_ascii=False), encoding="utf-8"
        )
        self.bank = ScoreBank(2.0)
        self.fake_feishu = FakeFeishuSyncClient(existing_records={"tbl-case": []})
        self.project_config = {
            "project_id": "e2e",
            "database_path": str(root / "var" / "db.sqlite3"),
            "artifact_root": str(root / "var" / "artifacts"),
            "manifest_root": str(root / "var" / "manifests"),
            "benchmark_path": str(ROOT / "BENCHMARK.md"),
            "scenario_pack_path": str(ROOT / "config" / "scenarios.json"),
            "operation_recipe_path": str(ROOT / "config" / "operation-recipes.json"),
            "default_model": "x2.0",
            "default_repeats": 1,
            "generation_modes": ["offline"],
            "paid_run_requires_approval": False,
        }
        benchmark = load_benchmark_contract(ROOT / "BENCHMARK.md")
        judges = [
            AllDimensionJudge(
                dim["dimension_id"],
                self.bank,
                dim.get("judge_routing", {}).get("primary_kinds", ["metric"])[0],
            )
            for dim in benchmark["dimensions"]
        ]
        self.inject = {
            "probe": FakeProbe(),
            "offline_transport": FakeOfflineTaskTransport(),
            "session_api": FakeSessionApiClient(),
            "rtc": FakeRtcAdapter(),
            "feishu_sync": self.fake_feishu,
            "judges": judges,
        }
        self.composition = Composition(
            root, project_config=self.project_config, inject=self.inject
        )
        self.root = root

    def tearDown(self) -> None:
        self.composition.close()
        self.directory.cleanup()

    # ------------------------------------------------------------------
    def seed_assets(self, prefix: str = "e2e") -> None:
        repo = self.composition.database
        media = {
            "duration_s": 8.0,
            "width": 704,
            "height": 1280,
            "fps": 24.0,
            "has_audio": True,
        }
        for asset_id, kind, content in [
            (f"{prefix}-feed", "feed_video", b"feed-bytes"),
            (f"{prefix}-prompt-img", "prompt_image", b"prompt-image-bytes"),
        ]:
            repo.upsert_asset(
                {
                    "asset_id": asset_id,
                    "kind": kind,
                    "uri": f"artifact://assets/{asset_id}/source.bin",
                    "sha256": f"sha-{asset_id}",
                    "bytes": len(content),
                    "status": "ready",
                    "media": media,
                    "metadata": {},
                }
            )
            path = self.composition.artifacts.resolve(f"artifact://assets/{asset_id}/source.bin")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        repo.upsert_asset(
            {
                "asset_id": f"{prefix}-prompt-text",
                "kind": "prompt_text",
                "uri": f"artifact://assets/{prefix}-prompt-text/prompt.txt",
                "sha256": "sha-text",
                "bytes": 8,
                "status": "ready",
                "media": {},
                "metadata": {
                    "text": "换装：请完成服装替换",
                    "play_name": "换装",
                    "group_id": f"{prefix}-g",
                },
            }
        )
        # Prompt image shares the group with the prompt text.
        repo.upsert_asset(
            {
                "asset_id": f"{prefix}-prompt-img",
                "kind": "prompt_image",
                "uri": f"artifact://assets/{prefix}-prompt-img/source.bin",
                "sha256": f"sha-{prefix}-prompt-img",
                "bytes": 8,
                "status": "ready",
                "media": media,
                "metadata": {"group_id": f"{prefix}-g"},
            }
        )

    def build_plan(self, model_id: str = "x2.0") -> dict:
        builder = self.composition.plan_builder()
        return builder.build(
            {
                "asset_batch_ids": ["assets-e2e"],
                "model_id": model_id,
                "repeat_count": 1,
                "generation_modes": ["offline"],
                "filters": {},
            }
        )

    def generate_plan(self, plan: dict, model_id: str, batch_id: str) -> list[dict]:
        adapter = self.composition.offline_adapter(run_batch_id=batch_id, model_id=model_id)
        runs = []
        for case in plan.get("cases", []):
            run = adapter.run_case(case)
            self.composition.database.set_run_batch_id(run["run_id"], batch_id)
            runs.append(run)
        return runs

    def evaluate_runs(self, runs: list[dict]) -> dict:
        preprocess = self.composition.preprocess_service()
        for run in runs:
            preprocess.build(run)
        orchestrator = self.composition.evaluation_orchestrator()
        return orchestrator.evaluate_runs(runs)

    def report(
        self,
        baseline_model_version: str,
        candidate_model_version: str,
        requested_scene_ids: list[str],
    ) -> dict:
        service = self.composition.reporting_service()
        return service.generate(
            comparison_id="model-update-e2e",
            baseline_model_version=baseline_model_version,
            candidate_model_version=candidate_model_version,
            requested_scene_ids=requested_scene_ids,
            template_path=ROOT / "report-templates" / "model-version-update-report.md",
            output_directory=self.root / "var" / "reports" / "model-version-updates",
        )


class FullPipelineTests(FakeRunE2ETestBase):
    def test_task_worker_runs_one_frozen_case_through_sync_and_reconcile(self) -> None:
        self.seed_assets("task-worker")
        plan = self.build_plan("x2.0")
        runtime = PipelineTaskRuntime(
            self.composition,
            lease_owner="e2e-worker",
            sync_policy="full",
            reconcile=True,
        )
        summary = TaskWorker(self.composition.database, runtime).run_batch(
            plan["task_batch_id"], lease_owner="e2e-worker"
        )
        self.assertEqual(summary["errors"], [], summary)
        self.assertEqual(summary["counts"], {"completed": len(plan["cases"])})
        tasks = self.composition.database.list_test_tasks(
            task_batch_id=plan["task_batch_id"]
        )
        self.assertTrue(all(task["result_refs"].get("run_id") for task in tasks))
        self.assertTrue(all(task["result_refs"].get("evaluation_id") for task in tasks))
        self.assertEqual(len(self.fake_feishu._tables["tbl-case"]), len(plan["cases"]))

    def test_unified_pipeline_syncs_and_reconciles_only_current_run_batch(self) -> None:
        self.seed_assets("sync-scope")
        plan = self.build_plan("x2.0")
        request = {
            "request_id": "sync-scope-e2e",
            "stages": ["generate", "preprocess", "evaluate", "sync", "reconcile"],
            "stage_inputs": {
                "generate": [{
                    "selector_id": "sync-scope-plan",
                    "state": "request",
                    "entity_type": "test_plan",
                    "match": {"ids": [plan["plan_id"]]},
                }]
            },
            "dependency_policy": "explicit_only",
            "missing_input_policy": "error",
            "sync_policy": "full",
            "generation_modes": ["offline"],
            "repeat_count": 1,
            "execution_mode": "streaming",
            "pipeline_queue_size": 1,
        }
        args = SimpleNamespace(
            dry_run=False,
            resume=False,
            smoke_limit=None,
            budget_approved=True,
            json=True,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = _execute_run_request(self.composition, request, args)
        self.assertEqual(exit_code, 0, output.getvalue())
        sync_manifests = self.composition.database.list_batch_manifests("sync_batch")
        self.assertEqual(len(sync_manifests), 1)
        self.assertEqual(len(sync_manifests[0]["item_ids"]), len(plan["cases"]))

    def test_unified_run_streams_and_keeps_batch_contracts(self) -> None:
        self.seed_assets()
        plan = self.composition.plan_builder().build(
            {
                "asset_batch_ids": ["assets-e2e"],
                "model_id": "x2.0",
                "repeat_count": 2,
                "generation_modes": ["offline"],
                "filters": {},
            }
        )
        request = {
            "request_id": "streaming-e2e",
            "stages": ["generate", "preprocess", "evaluate"],
            "stage_inputs": {
                "generate": [
                    {
                        "selector_id": "stream-plan",
                        "state": "request",
                        "entity_type": "test_plan",
                        "match": {"ids": [plan["plan_id"]]},
                    }
                ]
            },
            "dependency_policy": "explicit_only",
            "missing_input_policy": "error",
            "sync_policy": "none",
            "benchmark_path": "BENCHMARK.md",
            "scenario_pack_path": "config/scenarios.json",
            "generation_modes": ["offline"],
            "repeat_count": 2,
            "execution_mode": "streaming",
            "pipeline_queue_size": 1,
        }
        args = SimpleNamespace(
            dry_run=False,
            resume=False,
            smoke_limit=None,
            budget_approved=True,
            json=True,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = _execute_run_request(self.composition, request, args)
        self.assertEqual(exit_code, 0, output.getvalue())

        runs = self.composition.database.list_runs()
        self.assertEqual(len(runs), 2)
        results = self.composition.database.list_evaluation_results()
        self.assertEqual(len(results), 2)
        evaluation_batch_ids = {item["evaluation_batch_id"] for item in results}
        self.assertEqual(len(evaluation_batch_ids), 1)
        evaluation_batch_id = next(iter(evaluation_batch_ids))
        manifest = self.composition.database.get_batch_manifest(
            "evaluation_batch", evaluation_batch_id
        )
        self.assertEqual(set(manifest["item_ids"]), {item["evaluation_id"] for item in results})
        self.assertEqual(manifest["metadata"]["execution_mode"], "streaming")
        self.assertTrue(manifest["metadata"]["streamed_during_generation"])

        # Resume reuses completed generation, preprocessing and evaluation.
        args.resume = True
        output = io.StringIO()
        with redirect_stdout(output):
            resumed_exit = _execute_run_request(self.composition, request, args)
        self.assertEqual(resumed_exit, 0, output.getvalue())
        self.assertEqual(len(self.composition.database.list_runs()), 2)
        self.assertEqual(len(self.composition.database.list_evaluation_results()), 2)

    def test_assets_to_report_and_reconcile(self) -> None:
        self.seed_assets()
        plan = self.build_plan("x2.0")
        self.assertTrue(plan.get("cases"))
        scenario_ids = sorted({case["scenario_id"] for case in plan["cases"]})

        baseline = self.generate_plan(plan, "x2.0", "runs-baseline")
        self.assertEqual(len(baseline), len(plan["cases"]))
        self.assertTrue(all(run["status"] == "completed" for run in baseline))

        self.bank.score = 2.0
        baseline_summary = self.evaluate_runs(baseline)
        self.assertEqual(baseline_summary["failed"], 0)

        # Full sync to the fake Feishu, then reconcile must pass.
        selected = {
            item["run_id"]: item
            for item in self.composition.database.list_evaluation_results()
            if item["run_id"] in {run["run_id"] for run in baseline}
        }
        sync = self.composition.feishu_sync_service().sync_case_runs(
            baseline, evaluations=selected, policy="full"
        )
        self.assertEqual(sync["errors"], [])
        outcome = self.composition.feishu_reconcile().reconcile(
            evaluations=selected
        )
        self.assertTrue(outcome["ok"], outcome)

        # Model-update report (single model -> not comparable, still emitted).
        result = self.report("x2.0", "x2.0", scenario_ids)
        self.assertIn(result["status"], {"complete", "not_comparable", "partial"})
        report_dir = self.root / "var" / "reports" / "model-version-updates"
        self.assertTrue((report_dir / "model-update-e2e.md").is_file())
        self.assertTrue((report_dir / "model-update-e2e.json").is_file())
        markdown = (report_dir / "model-update-e2e.md").read_text(encoding="utf-8")
        self.assertNotIn("{{", markdown)

    def test_version_update_report_has_p0_p1_p2_buckets(self) -> None:
        self.seed_assets()
        plan = self.build_plan("x2.0")
        scenario_ids = sorted({case["scenario_id"] for case in plan["cases"]})
        baseline = self.generate_plan(plan, "x2.0", "runs-baseline")
        candidate_cases = [dict(case, model_id="x2.1") for case in plan["cases"]]
        adapter = self.composition.offline_adapter(run_batch_id="runs-candidate", model_id="x2.1")
        candidate = []
        for case in candidate_cases:
            run = adapter.run_case(case)
            self.composition.database.set_run_batch_id(run["run_id"], "runs-candidate")
            candidate.append(run)
        self.assertEqual(
            [r["case_number"] for r in baseline], [r["case_number"] for r in candidate]
        )
        self.assertTrue(all(run["model_id"] == "x2.1" for run in candidate))

        # Baseline full marks, candidate worse -> regressions appear.
        self.bank.score = 2.0
        self.evaluate_runs(baseline)
        self.bank.score = 1.0
        self.evaluate_runs(candidate)

        schema = self.composition.benchmark["score_schemas"][0]
        schema["comparison_policy"] = {
            "improvement_min_delta": 5.0,
            "tie_abs_delta_max": 2.0,
            "regression_min_delta": 5.0,
        }
        result = self.report("x2.0", "x2.1", scenario_ids)
        self.assertEqual(result["status"], "complete")
        self.assertGreater(result["p2_count"], 0)
        report_json = json.loads(
            (self.root / "var" / "reports" / "model-version-updates" / "model-update-e2e.json").read_text(encoding="utf-8")
        )
        for bucket in ("p0_improvements", "p1_ties", "p2_regressions"):
            self.assertIn(bucket, report_json)


class GenerateOnlyTests(FakeRunE2ETestBase):
    def test_unified_generate_only_never_starts_downstream_workers(self) -> None:
        self.seed_assets("unified-gen")
        plan = self.build_plan("x2.0")
        request = {
            "request_id": "generate-only-stream-setting",
            "stages": ["generate"],
            "stage_inputs": {
                "generate": [
                    {
                        "selector_id": "generate-only-plan",
                        "state": "request",
                        "entity_type": "test_plan",
                        "match": {"ids": [plan["plan_id"]]},
                    }
                ]
            },
            "dependency_policy": "explicit_only",
            "missing_input_policy": "error",
            "sync_policy": "none",
            "benchmark_path": "BENCHMARK.md",
            "scenario_pack_path": "config/scenarios.json",
            "execution_mode": "streaming",
            "pipeline_queue_size": 1,
        }
        args = SimpleNamespace(
            dry_run=False,
            resume=False,
            smoke_limit=None,
            budget_approved=True,
            json=True,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = _execute_run_request(self.composition, request, args)
        self.assertEqual(exit_code, 0, output.getvalue())
        self.assertTrue(self.composition.database.list_runs())
        self.assertEqual(self.composition.database.list_evaluation_results(), [])
        preprocess_count = self.composition.database._conn.execute(
            "SELECT count(*) FROM preprocess_runs"
        ).fetchone()[0]
        self.assertEqual(preprocess_count, 0)

    def test_generate_only_no_evaluations(self) -> None:
        self.seed_assets()
        plan = self.build_plan("x2.0")
        runs = self.generate_plan(plan, "x2.0", "runs-gen-only")
        self.assertTrue(runs)
        # No evaluation side effects.
        self.assertEqual(
            len(self.composition.database.list_evaluation_results()), 0
        )


class EvaluateOnlyTests(FakeRunE2ETestBase):
    def test_old_runs_evaluate_only(self) -> None:
        self.seed_assets()
        plan = self.build_plan("x2.0")
        runs = self.generate_plan(plan, "x2.0", "runs-old")
        before = len(self.composition.database.list_runs())
        self.bank.score = 2.0
        summary = self.evaluate_runs(runs)
        self.assertEqual(summary["failed"], 0)
        # Evaluation never re-runs generation or adds runs.
        self.assertEqual(len(self.composition.database.list_runs()), before)
        self.assertTrue(summary["results"])


class ImportThenEvaluateTests(FakeRunE2ETestBase):
    def test_import_feishu_cases_evaluate_without_writing_remote(self) -> None:
        record = {
            "record_id": "rec-import-1",
            "fields": {
                "case编号": "feed001_prompt001_01",
                "Xmax模型版本": "x2.0",
                "prompt文字": "换装：请完成服装替换",
                "case文件": [{"file_token": "tok-result", "name": "result.mp4"}],
                "feed文件": [{"file_token": "tok-feed", "name": "feed.mp4"}],
                "prompt素材": [{"file_token": "tok-prompt", "name": "prompt.jpg"}],
            },
        }
        self.fake_feishu._tables["tbl-case"] = [record]

        services = self.composition.ingest_service()
        outcome = services["importer"].import_results(
            IMPORT_CONFIG, feishu_client=self.fake_feishu
        )
        self.assertEqual(outcome.status, "completed", outcome.errors)
        self.assertEqual(outcome.imported, 1)

        runs = self.composition.database.list_runs(run_batch_id=outcome.run_batch_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["origin"], "feishu_import")

        self.bank.score = 2.0
        summary = self.evaluate_runs(runs)
        self.assertEqual(summary["failed"], 0)
        self.assertTrue(summary["results"])

        # Reads happened, but nothing was written back to remote.
        self.assertNotIn("upsert_record", self.fake_feishu.calls)
        self.assertNotIn("upload_attachment", self.fake_feishu.calls)
        self.assertEqual(self.fake_feishu.upserts, [])


class ScoreOnlySyncTests(FakeRunE2ETestBase):
    def test_score_only_write_back(self) -> None:
        self.seed_assets()
        plan = self.build_plan("x2.0")
        runs = self.generate_plan(plan, "x2.0", "runs-score")
        self.bank.score = 2.0
        evaluation_summary = self.evaluate_runs(runs)

        # Remote already has the Case records.
        self.fake_feishu._tables["tbl-case"] = [
            {
                "record_id": "rec-score-1",
                "fields": {"case编号": runs[0]["case_number"], "Xmax模型版本": "x2.0"},
            }
        ]
        sync = self.composition.feishu_sync_service().sync_case_runs(
            runs,
            evaluations={item["run_id"]: item for item in evaluation_summary["results"]},
            policy="score_only",
        )
        self.assertEqual(sync["errors"], [])
        self.assertEqual(sync["created"], 0)
        # No attachments were uploaded for score_only.
        self.assertEqual(self.fake_feishu.uploads, [])
        remote = self.fake_feishu._tables["tbl-case"][0]["fields"]
        self.assertIn("case评分", remote)


if __name__ == "__main__":
    unittest.main()
