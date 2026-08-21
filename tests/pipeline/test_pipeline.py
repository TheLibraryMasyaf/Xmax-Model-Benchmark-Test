"""P0.7 Stage orchestration tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from xmax_test.contracts import PipelineStage
from xmax_test.errors import (
    ApprovalRequiredError,
    ContractError,
    MissingInputError,
    PartialCompletionError,
)
from xmax_test.pipeline.dependencies import check_stage_inputs, raise_missing_inputs
from xmax_test.pipeline.manifests import (
    ManifestStore,
    build_batch_manifest,
    build_stage_manifest,
)
from xmax_test.pipeline.models import (
    StageExecutionRequest,
    StageExecutionResult,
    topological_sort,
)
from xmax_test.pipeline.orchestrator import PipelineOrchestrator
from xmax_test.pipeline.selectors import SelectorResolver
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.time import FixedClock

SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas"


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


class RecordingExecutor:
    def __init__(self, stage: str, calls: list[str]) -> None:
        self.stage = PipelineStage(stage)
        self._calls = calls

    def execute(self, request: StageExecutionRequest) -> StageExecutionResult:
        self._calls.append(self.stage.value)
        return StageExecutionResult(
            status="completed",
            output_refs=[],
            batch_manifests=[
                build_batch_manifest(
                    entity_type=_output_of(self.stage.value),
                    item_entity_type="test",
                    item_ids=[f"{self.stage.value}-item-1"],
                    producer_stage_run_id=request.stage_run_id,
                )
            ],
        )


def _output_of(stage: str) -> str:
    from xmax_test.pipeline.models import STAGE_OUTPUTS

    return STAGE_OUTPUTS[stage]


class PipelineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(self.root / "db.sqlite3", clock=FixedClock())
        self.manifest_store = ManifestStore(self.root / "manifests", self.repository)
        self.selectors = SelectorResolver(self.repository, self.root / "manifests")
        self.calls: list[str] = []

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def orchestrator(self, stages: list[str]) -> PipelineOrchestrator:
        executors = {PipelineStage(stage): RecordingExecutor(stage, self.calls) for stage in stages}
        return PipelineOrchestrator(self.repository, self.manifest_store, self.selectors, executors)

    def request(self, stages: list[str], **extra) -> dict:
        request: dict = {
            "request_id": "test-request",
            "stages": stages,
            "stage_inputs": {},
            "dependency_policy": "explicit_only",
            "missing_input_policy": "error",
            "sync_policy": "none",
        }
        request.update(extra)
        return request

    def seed_asset_batch(self) -> str:
        manifest = build_batch_manifest(
            entity_type="asset_batch",
            item_entity_type="asset",
            item_ids=["asset-1"],
            producer_stage_run_id="stage-seed",
        )
        self.repository.save_batch_manifest(manifest)
        return manifest["batch_id"]

    def seed_plan(self) -> dict:
        plan = {
            "plan_id": "plan-seed",
            "plan_version": "1",
            "plan_hash": "plan-hash-seed",
            "frozen": True,
            "cases": [
                {
                    "case_id": "case-seed",
                    "case_number": "feed001_prompt001_01",
                    "feed_number": "feed001",
                    "prompt_number": "prompt001",
                    "feed_asset_id": "asset-1",
                    "prompt_text": "do it",
                    "generation_mode": "offline",
                    "repeat_index": 1,
                    "model_id": "x2.0",
                    "operation_recipe_id": "r",
                    "operation_recipe_version": "1",
                    "edited_video_asset_id": "asset-1",
                    "expected_audio_source_asset_id": "asset-1",
                }
            ],
        }
        self.repository.save_test_plan(plan)
        return plan

    def selector(self, entity_type: str, ids: list[str]) -> dict:
        return {
            "selector_id": f"sel-{entity_type}",
            "state": "request",
            "entity_type": entity_type,
            "match": {"ids": ids},
        }


class SelectorTests(PipelineTestBase):
    def test_filters_resolve_to_frozen_snapshot(self) -> None:
        self.repository.create_run(
            {
                "run_id": "run-1",
                "run_batch_id": "batch-x",
                "case_id": "case-1",
                "case_number": "feed001_prompt001_01",
                "model_id": "x2.0",
                "mode": "offline",
                "origin": "xmax_offline",
                "status": "completed",
                "provenance": {
                    "source_type": "t",
                    "source_locator": "l",
                    "source_hash": "h",
                },
            }
        )
        frozen = self.selectors.resolve(
            {
                "selector_id": "sel-1",
                "state": "request",
                "entity_type": "run_batch",
                "match": {"filters": {"model_version": "x2.0"}},
            }
        )
        self.assertEqual(frozen["state"], "frozen")
        self.assertEqual(frozen["resolved_entity_ids"], ["batch-x"])
        self.assertTrue(frozen["snapshot_hash"])

    def test_frozen_selector_is_validated(self) -> None:
        with self.assertRaises(ContractError):
            self.selectors.resolve(
                {
                    "selector_id": "bad",
                    "state": "frozen",
                    "entity_type": "run_batch",
                    "match": {"ids": ["x"]},
                }
            )

    def test_unknown_ids_raise_missing_input(self) -> None:
        with self.assertRaises(MissingInputError):
            self.selectors.resolve(
                {
                    "selector_id": "sel-2",
                    "state": "request",
                    "entity_type": "run_batch",
                    "match": {"ids": ["nope"]},
                }
            )


class DependencyTests(PipelineTestBase):
    def test_missing_dependency_returns_suggested_command(self) -> None:
        problems = check_stage_inputs("evaluate", {}, authorized_stages=set())
        self.assertEqual(len(problems), 2)
        messages = {item["message"] for item in problems}
        self.assertIn("stage evaluate requires run_batch", messages)
        self.assertIn("stage evaluate requires preprocess_batch", messages)
        self.assertTrue(all(item["suggested_command"] for item in problems))

    def test_missing_input_raises_with_suggestion(self) -> None:
        with self.assertRaises(MissingInputError) as caught:
            raise_missing_inputs("generate", {}, authorized_stages=set())
        self.assertIn("suggested_command", caught.exception.to_dict())


class OrchestratorTests(PipelineTestBase):
    def test_sync_consumes_exact_evaluation_batch_without_duplicate_refs(self) -> None:
        stages = ["plan", "generate", "preprocess", "evaluate", "sync"]
        asset_batch = self.seed_asset_batch()
        seen_sync_refs: list[tuple[str, str]] = []

        class CapturingSyncExecutor(RecordingExecutor):
            def execute(self, request: StageExecutionRequest) -> StageExecutionResult:
                seen_sync_refs.extend(
                    (ref.entity_type, ref.entity_id) for ref in request.input_refs
                )
                return super().execute(request)

        executors = {PipelineStage(stage): RecordingExecutor(stage, self.calls) for stage in stages}
        executors[PipelineStage.SYNC] = CapturingSyncExecutor("sync", self.calls)
        orchestrator = PipelineOrchestrator(
            self.repository, self.manifest_store, self.selectors, executors
        )

        summary = orchestrator.run(
            self.request(
                stages,
                stage_inputs={"plan": [self.selector("asset_batch", [asset_batch])]},
                sync_policy="full",
            ),
            budget_approved=True,
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(
            [entity_type for entity_type, _ in seen_sync_refs],
            ["run_batch", "evaluation_batch"],
        )
        self.assertEqual(len(seen_sync_refs), len(set(seen_sync_refs)))

    def test_dry_run_publishes_placeholders_for_no_output_executors(self) -> None:
        stages = ["ingest", "plan", "generate", "preprocess", "evaluate"]

        class NoOutputExecutor:
            def __init__(self, stage: str, calls: list[str]) -> None:
                self.stage = PipelineStage(stage)
                self.calls = calls

            def execute(self, request: StageExecutionRequest) -> StageExecutionResult:
                self.calls.append(self.stage.value)
                return StageExecutionResult(status="completed")

        orchestrator = PipelineOrchestrator(
            self.repository,
            self.manifest_store,
            self.selectors,
            {PipelineStage(stage): NoOutputExecutor(stage, self.calls) for stage in stages},
        )
        summary = orchestrator.run(self.request(stages), dry_run=True)
        self.assertTrue(summary["ok"])
        self.assertEqual(self.calls, stages)
        evaluate = self.repository.get_stage_run(summary["executed"][-1]["stage_run_id"])
        self.assertEqual(evaluate["output_refs"][0]["entity_type"], "evaluation_batch")

    def test_topological_order_is_stable(self) -> None:
        self.assertEqual(
            topological_sort(["evaluate", "plan", "generate", "preprocess"]),
            ["plan", "generate", "preprocess", "evaluate"],
        )
        self.assertEqual(topological_sort(["report", "evaluate"]), ["evaluate", "report"])

    def test_full_chain_executes_in_order(self) -> None:
        stages = ["plan", "generate", "preprocess", "evaluate"]
        asset_batch = self.seed_asset_batch()
        orchestrator = self.orchestrator(stages)
        summary = orchestrator.run(
            self.request(
                stages,
                stage_inputs={"plan": [self.selector("asset_batch", [asset_batch])]},
            ),
            budget_approved=True,
        )
        self.assertTrue(summary["ok"])
        self.assertEqual([item["stage"] for item in summary["executed"]], stages)
        self.assertEqual(self.calls, stages)
        # Batch manifests were persisted (seed + 4 stages).
        manifests = self.repository.list_batch_manifests()
        self.assertEqual(len(manifests), 5)

    def test_same_hash_skips_completed_stage(self) -> None:
        stages = ["plan", "generate"]
        asset_batch = self.seed_asset_batch()
        orchestrator = self.orchestrator(stages)
        inputs = {"plan": [self.selector("asset_batch", [asset_batch])]}
        first = orchestrator.run(self.request(stages, stage_inputs=inputs), budget_approved=True)
        self.assertEqual([item["status"] for item in first["executed"]], ["completed", "completed"])
        second = orchestrator.run(self.request(stages, stage_inputs=inputs), budget_approved=True)
        self.assertEqual([item["status"] for item in second["executed"]], ["skipped", "skipped"])
        self.assertEqual(self.calls, stages)  # second run executed nothing

    def test_resume_reuses_completed_stage_and_only_runs_missing(self) -> None:
        """--resume must reuse unchanged completed stages instead of re-running
        them.  Re-executing a completed plan stage would renumber Case suffixes
        and collide with the already-persisted task payloads."""

        stages = ["plan", "generate", "preprocess", "evaluate"]
        asset_batch = self.seed_asset_batch()
        orchestrator = self.orchestrator(stages)
        inputs = {"plan": [self.selector("asset_batch", [asset_batch])]}
        request = self.request(stages, stage_inputs=inputs)

        first = orchestrator.run(request, budget_approved=True)
        self.assertEqual([item["status"] for item in first["executed"]], ["completed"] * 4)
        self.assertEqual(self.calls, stages)

        # Resume with identical hashes: every completed stage is reused.
        resumed = orchestrator.run(request, resume=True, budget_approved=True)
        self.assertEqual(
            [item["status"] for item in resumed["executed"]],
            ["skipped", "skipped", "skipped", "skipped"],
        )
        self.assertEqual(self.calls, stages)  # resume executed nothing again

        # An incomplete stage is the only one that runs on resume.
        third = orchestrator.run(
            {**request, "stages": ["plan", "generate", "preprocess"]},
            resume=True,
            budget_approved=True,
        )
        self.assertEqual(
            [item["status"] for item in third["executed"]],
            ["skipped", "skipped", "skipped"],
        )
        self.assertEqual(self.calls, stages)

    def test_changed_config_creates_new_run_for_generate(self) -> None:
        stages = ["generate"]
        plan = self.seed_plan()
        orchestrator = self.orchestrator(stages)
        inputs = {"generate": [self.selector("test_plan", [plan["plan_id"]])]}
        orchestrator.run(self.request(stages, stage_inputs=inputs), budget_approved=True)
        self.assertEqual(self.calls, ["generate"])
        orchestrator.run(
            self.request(stages, stage_inputs=inputs, repeat_count=3),
            budget_approved=True,
        )
        self.assertEqual(self.calls, ["generate", "generate"])

    def test_missing_dependency_stops_without_executing(self) -> None:
        stages = ["generate"]
        orchestrator = self.orchestrator(stages)
        # No test_plan selector -> generate must not run and must report the
        # missing input instead of silently executing.
        with self.assertRaises(PartialCompletionError):
            orchestrator.run(self.request(stages), budget_approved=True)
        self.assertEqual(self.calls, [])

    def test_generate_without_approval_is_blocked(self) -> None:
        stages = ["generate"]
        orchestrator = self.orchestrator(stages)
        with self.assertRaises(ApprovalRequiredError):
            orchestrator.run(self.request(stages))  # dry_run defaults False
        self.assertEqual(self.calls, [])  # executor never invoked

    def test_evaluate_never_calls_generation_adapter(self) -> None:
        """Executors are isolated: evaluate must not invoke generate."""

        generate_executor = RecordingExecutor("generate", self.calls)

        class EvaluateExecutor:
            stage = PipelineStage("evaluate")

            def execute(self, request: StageExecutionRequest) -> StageExecutionResult:
                return StageExecutionResult(status="completed", output_refs=[])

        # The orchestrator receives a real generate executor but a request
        # that only authorizes evaluate. Missing inputs must be reported
        # without touching the generate executor.
        orchestrator = PipelineOrchestrator(
            self.repository,
            self.manifest_store,
            self.selectors,
            {PipelineStage("generate"): generate_executor},
        )
        with self.assertRaises(PartialCompletionError):
            orchestrator.run(self.request(["evaluate"]))
        self.assertEqual(self.calls, [])


class ManifestSchemaTests(PipelineTestBase):
    def test_stage_manifest_validates_against_schema(self) -> None:
        manifest = build_stage_manifest(
            stage="plan",
            input_refs=(),
            output_refs=[],
            input_hash="in",
            config_hash="cfg",
            status="completed",
        )
        schema = _load_schema("stage-manifest.schema.json")
        Draft202012Validator(schema).validate(manifest)

    def test_batch_manifest_validates_against_schema(self) -> None:
        manifest = build_batch_manifest(
            entity_type="run_batch",
            item_entity_type="generation_run",
            item_ids=["run-1", "run-2"],
            producer_stage_run_id="stage-1",
        )
        schema = _load_schema("batch-manifest.schema.json")
        Draft202012Validator(schema).validate(manifest)


if __name__ == "__main__":
    unittest.main()
