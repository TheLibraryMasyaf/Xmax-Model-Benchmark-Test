"""P3 Planning tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from xmax_test.benchmark import load_benchmark_contract
from xmax_test.planning.budget import BudgetPreview
from xmax_test.planning.builder import TestPlanBuilder
from xmax_test.planning.case_numbers import CaseNumberAllocator
from xmax_test.planning.recipes import RecipeResolver
from xmax_test.planning.strategies import SelectedCombination, StrategyRegistry
from xmax_test.scenarios import load_scenario_pack
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.time import FixedClock

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = ROOT / "schemas"


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


class PlanningTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repository = SqliteMetadataRepository(root / "db.sqlite3", clock=FixedClock())
        self.benchmark = load_benchmark_contract(ROOT / "BENCHMARK.md")
        self.pack = load_scenario_pack(ROOT / "config" / "scenarios.json")
        self.recipes = RecipeResolver(ROOT / "config" / "operation-recipes.json")
        self.allocator = CaseNumberAllocator(self.repository)
        self.builder = TestPlanBuilder(
            self.repository,
            self.recipes,
            self.pack,
            self.benchmark,
            allocator=self.allocator,
            clock=FixedClock(),
        )
        self._register_assets()

    def tearDown(self) -> None:
        self.repository.close()
        self.directory.cleanup()

    def _register_assets(self) -> None:
        def asset(asset_id: str, kind: str, metadata: dict | None = None) -> None:
            self.repository.upsert_asset(
                {
                    "asset_id": asset_id,
                    "kind": kind,
                    "uri": f"artifact://assets/{asset_id}/source.bin",
                    "sha256": f"sha-{asset_id}",
                    "bytes": 10,
                    "status": "ready",
                    "metadata": metadata or {},
                }
            )

        asset("feed-1", "feed_video")
        asset("feed-2", "feed_video")
        asset(
            "prompt-1",
            "prompt_text",
            {
                "text": "换装：把人物替换为参考图的服装",
                "group_id": "g1",
                "record_number": 1,
                "scenario_id": "core-indoor-selfie-person-replacement",
            },
        )
        asset("prompt-ref-1", "prompt_image", {"group_id": "g1"})
        asset(
            "prompt-2",
            "prompt_text",
            {
                "text": "手势舞：按照参考视频完成动作",
                "group_id": "g2",
                "record_number": 2,
                "scenario_id": "core-high-speed-subject-edit",
            },
        )
        asset("prompt-ref-2", "prompt_video", {"group_id": "g2"})

    def request(self, **extra) -> dict:
        request: dict = {
            "asset_batch_ids": ["assets-batch-1"],
            "model_id": "x2.0",
            "repeat_count": 5,
            "generation_modes": ["offline", "realtime"],
            "generation_mode_overrides": [],
            "filters": {},
        }
        request.update(extra)
        return request


class DeterminismTests(PlanningTestBase):
    def test_unrelated_asset_batch_lineage_does_not_change_plan_identity(self) -> None:
        first = self.builder.build(self.request(asset_batch_ids=["assets-before"], repeat_count=1))
        second = self.builder.build(self.request(asset_batch_ids=["assets-after"], repeat_count=1))
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(
            [case["case_id"] for case in first["cases"]],
            [case["case_id"] for case in second["cases"]],
        )
        self.assertTrue(all(case.get("generation_signature") for case in first["cases"]))

    def test_rebuild_reuses_frozen_plan_instead_of_renumbering_cases(self) -> None:
        """A crashed run must not re-allocate Case suffixes on rebuild: the
        frozen plan and its task batch stay identical so resume never hits a
        duplicate-task payload conflict."""

        first = self.builder.build(self.request(seed=7, repeat_count=2))
        plan_id = first["plan_id"]
        plan_hash = first["plan_hash"]
        case_numbers = [case["case_number"] for case in first["cases"]]
        task_ids = list(first.get("task_ids", []))

        # Simulate a mid-batch crash + resume: rebuild with the same inputs.
        rebuilt = self.builder.build(self.request(seed=7, repeat_count=2))
        self.assertEqual(rebuilt["plan_id"], plan_id)
        self.assertEqual(rebuilt["plan_hash"], plan_hash)
        self.assertEqual(
            [case["case_number"] for case in rebuilt["cases"]], case_numbers
        )
        self.assertEqual(list(rebuilt.get("task_ids", [])), task_ids)
        # Rebuild did not create new tasks; the original task set is reused.
        tasks = self.repository.list_test_tasks(task_batch_id=rebuilt["task_batch_id"])
        self.assertEqual(len(tasks), len(task_ids))
        self.assertEqual({task["task_id"] for task in tasks}, set(task_ids))

    def test_first_n_filters_are_deterministic_and_change_plan_hash(self) -> None:
        full = self.builder.build(self.request(seed=42, repeat_count=1))
        limited = self.builder.build(
            self.request(
                seed=42,
                repeat_count=1,
                filters={"feed_limit": 1, "prompt_limit": 1},
            )
        )
        self.assertNotEqual(full["plan_hash"], limited["plan_hash"])
        self.assertEqual({case["feed_asset_id"] for case in limited["cases"]}, {"feed-1"})
        self.assertEqual(len({case["prompt_number"] for case in limited["cases"]}), 1)

    def test_content_addressed_asset_bindings_preserve_prompt_role(self) -> None:
        physical = self.repository.get_asset("feed-1")
        physical["metadata"] = {
            "bindings": [
                {"kind": "feed_video", "record_id": "feed-record"},
                {"kind": "prompt_image", "record_id": "g3", "group_id": "g3"},
            ]
        }
        self.repository.upsert_asset(physical)
        self.repository.upsert_asset(
            {
                "asset_id": "prompt-3-bound",
                "kind": "prompt_text",
                "uri": "artifact://assets/prompt-3-bound/source.bin",
                "sha256": "sha-prompt-3-bound",
                "bytes": 10,
                "status": "ready",
                "metadata": {
                    "text": "换装：按参考图替换服装",
                    "group_id": "g3",
                    "scenario_id": "core-indoor-selfie-person-replacement",
                },
            }
        )
        plan = self.builder.build(self.request(seed=31, repeat_count=1))
        bound = [case for case in plan["cases"] if case["prompt_text"] == "换装：按参考图替换服装"]
        self.assertTrue(bound)
        self.assertTrue(all(case["prompt_asset_ids"] == ["feed-1"] for case in bound))

    def test_same_inputs_same_case_ids(self) -> None:
        first = self.builder.build(self.request(seed=42))
        second = self.builder.build(self.request(seed=42))
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(
            [case["case_id"] for case in first["cases"]],
            [case["case_id"] for case in second["cases"]],
        )
        self.assertEqual(
            [case["case_number"] for case in first["cases"]],
            [case["case_number"] for case in second["cases"]],
        )

    def test_different_seed_changes_plan(self) -> None:
        first = self.builder.build(self.request(seed=1))
        second = self.builder.build(self.request(seed=2))
        self.assertNotEqual(first["plan_hash"], second["plan_hash"])

    def test_default_repeats_is_five_and_overridable(self) -> None:
        plan = self.builder.build(self.request(seed=7))
        repeat_indices = [case["repeat_index"] for case in plan["cases"]]
        self.assertEqual(repeat_indices, [1, 2, 3, 4, 5] * (len(plan["cases"]) // 5))
        plan2 = self.builder.build(self.request(seed=7, repeat_count=2))
        repeat_indices2 = [case["repeat_index"] for case in plan2["cases"]]
        self.assertEqual(repeat_indices2, [1, 2] * (len(plan2["cases"]) // 2))

    def test_case_numbers_follow_pattern(self) -> None:
        plan = self.builder.build(self.request(seed=7))
        pattern = r"^feed\d{3}_prompt\d{3}(?:_\d{2,})?$"
        for case in plan["cases"]:
            self.assertRegex(case["case_number"], pattern)


class AllocationStrategyTests(PlanningTestBase):
    def test_random_runs_selects_exact_attempt_count_and_is_deterministic(self) -> None:
        request = self.request(
            seed=91,
            repeat_count=5,
            combination_selection={"strategy": "random_runs", "target_run_count": 7},
        )
        first = self.builder.build(request)
        second = self.builder.build(request)
        self.assertEqual(len(first["cases"]), 7)
        self.assertEqual(
            [case["case_id"] for case in first["cases"]],
            [case["case_id"] for case in second["cases"]],
        )
        self.assertEqual(first["metadata"]["effective_repeat_count"], 1)

    def test_random_pairs_applies_configurable_repeat_count(self) -> None:
        plan = self.builder.build(
            self.request(
                seed=19,
                repeat_count=3,
                combination_selection={
                    "strategy": "random_pairs",
                    "target_pair_count": 2,
                    "with_replacement": False,
                },
            )
        )
        self.assertEqual(len(plan["cases"]), 6)
        self.assertEqual(plan["metadata"]["allocation"]["selected_pair_count"], 2)

    def test_explicit_pairs_do_not_form_a_cartesian_product(self) -> None:
        plan = self.builder.build(
            self.request(
                seed=5,
                repeat_count=2,
                combination_selection={
                    "strategy": "explicit_pairs",
                    "pairs": [
                        {"feed_asset_id": "feed-1", "prompt_record_number": 1},
                        {"feed_asset_id": "feed-2", "prompt_record_number": 2},
                    ],
                },
            )
        )
        self.assertEqual(len(plan["cases"]), 4)
        actual = {(case["feed_asset_id"], case["prompt_number"]) for case in plan["cases"]}
        self.assertEqual(actual, {("feed-1", "prompt001"), ("feed-2", "prompt002")})

    def test_custom_strategy_can_be_registered_without_builder_changes(self) -> None:
        class FirstOnly:
            name = "first_only"
            version = "test"

            def select(self, candidates, selection, *, repeat_count, seed):
                return [SelectedCombination(candidates[0], 1, 1, 1)]

        registry = StrategyRegistry.defaults()
        registry.register(FirstOnly())
        builder = TestPlanBuilder(
            self.repository,
            self.recipes,
            self.pack,
            self.benchmark,
            allocator=self.allocator,
            strategy_registry=registry,
            clock=FixedClock(),
        )
        plan = builder.build(self.request(combination_selection={"strategy": "first_only"}))
        self.assertEqual(len(plan["cases"]), 1)


class RecipeModeTests(PlanningTestBase):
    def test_missing_scenario_is_skipped_instead_of_using_first_scene(self) -> None:
        self.repository.upsert_asset(
            {
                "asset_id": "prompt-no-scene",
                "kind": "prompt_text",
                "uri": "artifact://assets/prompt-no-scene/source.bin",
                "sha256": "sha-prompt-no-scene",
                "bytes": 10,
                "status": "ready",
                "metadata": {"text": "换装：不要猜场景", "group_id": "no-scene"},
            }
        )
        self.repository.upsert_asset(
            {
                "asset_id": "prompt-no-scene-ref",
                "kind": "prompt_image",
                "uri": "artifact://assets/prompt-no-scene-ref/source.bin",
                "sha256": "sha-prompt-no-scene-ref",
                "bytes": 10,
                "status": "ready",
                "metadata": {"group_id": "no-scene"},
            }
        )
        plan = self.builder.build(self.request(seed=99, repeat_count=1))
        self.assertFalse(
            any(case["prompt_text"] == "换装：不要猜场景" for case in plan["cases"])
        )
        self.assertTrue(
            any(
                "must provide an explicit scenario_id" in item.get("reason", "")
                for item in plan["metadata"]["skipped"]
            )
        )

    def test_offline_image_reference_uses_feed_as_edited(self) -> None:
        plan = self.builder.build(self.request(seed=3))
        image_case = next(
            case
            for case in plan["cases"]
            if case["operation_recipe_id"] == "offline-image-reference"
        )
        self.assertEqual(image_case["generation_mode"], "offline")
        self.assertEqual(image_case["edited_video_asset_id"], image_case["feed_asset_id"])
        self.assertEqual(image_case["expected_audio_source_asset_id"], image_case["feed_asset_id"])
        self.assertEqual(
            image_case["api_asset_bindings"],
            {"refVideoPath": "feed_video", "refImagePath": "prompt_image"},
        )

    def test_video_reference_uses_prompt_video_as_edited(self) -> None:
        plan = self.builder.build(self.request(seed=3))
        video_case = next(
            case
            for case in plan["cases"]
            if case["operation_recipe_id"] == "offline-video-reference-with-feed-capture"
        )
        self.assertEqual(video_case["generation_mode"], "offline")
        self.assertEqual(video_case["edited_video_asset_id"], "prompt-ref-2")
        self.assertEqual(video_case["expected_audio_source_asset_id"], "prompt-ref-2")
        self.assertEqual(
            video_case["api_asset_bindings"],
            {"refVideoPath": "prompt_video", "refImagePath": "feed_capture"},
        )

    def test_interactive_recipe_defaults_to_realtime(self) -> None:
        # A prompt whose text contains an interactive play name resolves to a
        # realtime recipe by default.
        self.repository.upsert_asset(
            {
                "asset_id": "prompt-3",
                "kind": "prompt_text",
                "uri": "artifact://assets/prompt-3/source.bin",
                "sha256": "sha-prompt-3",
                "bytes": 10,
                "status": "ready",
                "metadata": {
                    "text": "视频中动物跟随轨迹运动",
                    "scenario_id": "core-moving-camera-effects",
                },
            }
        )
        plan = self.builder.build(self.request(seed=5))
        interactive = [
            case for case in plan["cases"] if case["prompt_text"] == "视频中动物跟随轨迹运动"
        ]
        self.assertTrue(interactive)
        self.assertTrue(all(case["generation_mode"] == "realtime" for case in interactive))
        self.assertTrue(
            all(
                case["api_asset_bindings"]["input_media_role"] == "feed_capture"
                for case in interactive
            )
        )
        self.assertTrue(
            all(
                case["api_asset_bindings"]["capture_frame_policy"]
                == "seeded_random_safe_window_v1"
                for case in interactive
            )
        )

    def test_explicit_override_wins_over_recipe_default(self) -> None:
        request = self.request(
            seed=5,
            generation_mode_overrides=[{"play_name": "换装", "mode": "realtime"}],
        )
        plan = self.builder.build(request)
        image_case = next(
            case
            for case in plan["cases"]
            if case["operation_recipe_id"] == "offline-image-reference"
        )
        self.assertEqual(image_case["generation_mode"], "realtime")

    def test_unsupported_mode_override_is_skipped(self) -> None:
        request = self.request(
            seed=5,
            generation_mode_overrides=[{"play_name": "手势舞", "mode": "realtime"}],
        )
        plan = self.builder.build(request)
        self.assertGreaterEqual(plan["metadata"]["skipped_count"], 1)
        skipped_reasons = [item["reason"] for item in plan["metadata"]["skipped"]]
        self.assertTrue(any("does not allow explicit mode" in reason for reason in skipped_reasons))


class NumberingTests(PlanningTestBase):
    def test_bare_historical_attempt_continues_at_suffix_two(self) -> None:
        self.repository.create_run(
            {
                "run_id": "run-bare",
                "run_batch_id": "b",
                "case_id": "c-bare",
                "case_number": "feed001_prompt001",
                "model_id": "x2.0",
                "mode": "offline",
                "origin": "xmax_offline",
                "status": "error",
                "provenance": {
                    "source_type": "t",
                    "source_locator": "l",
                    "source_hash": "h",
                },
            }
        )
        plan = self.builder.build(self.request(seed=9, repeat_count=1))
        case = next(
            item for item in plan["cases"] if item["case_number"].startswith("feed001_prompt001")
        )
        self.assertEqual(case["case_number"], "feed001_prompt001_02")

    def test_repeat_groups_continue_from_max_suffix(self) -> None:
        # Simulate history: two previous runs exist for feed001_prompt001.
        self.repository.create_run(
            {
                "run_id": "run-old-1",
                "run_batch_id": "b",
                "case_id": "c",
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
        self.repository.create_run(
            {
                "run_id": "run-old-2",
                "run_batch_id": "b",
                "case_id": "c",
                "case_number": "feed001_prompt001_02",
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
        plan = self.builder.build(self.request(seed=9, repeat_count=5))
        feed1_prompt1 = [
            case["case_number"]
            for case in plan["cases"]
            if case["case_number"].startswith("feed001_prompt001")
        ]
        self.assertEqual(
            feed1_prompt1,
            [
                "feed001_prompt001_03",
                "feed001_prompt001_04",
                "feed001_prompt001_05",
                "feed001_prompt001_06",
                "feed001_prompt001_07",
            ],
        )

    def test_allocator_single_repeat_without_history_has_no_suffix(self) -> None:
        allocator = CaseNumberAllocator()
        self.assertEqual(
            allocator.allocate(
                "feed001_prompt001",
                repeat_index=1,
                repeat_count=1,
                existing_max_suffix=0,
            ),
            "feed001_prompt001",
        )
        self.assertEqual(
            allocator.allocate(
                "feed001_prompt001",
                repeat_index=3,
                repeat_count=5,
                existing_max_suffix=2,
            ),
            "feed001_prompt001_05",
        )


class PreviewTests(PlanningTestBase):
    def test_documented_per_case_estimates_are_used_when_available(self) -> None:
        preview = BudgetPreview().preview(
            cases=[
                {
                    "generation_mode": "offline",
                    "generation_config": {
                        "estimated_credits": 12,
                        "estimated_billable_duration_s": 8.4,
                    },
                },
                {
                    "generation_mode": "realtime",
                    "generation_config": {
                        "estimated_credits": 3,
                        "estimated_billable_duration_s": 3,
                    },
                },
            ],
            skipped=[],
            repeat_count=1,
        )
        self.assertEqual(preview["expected_credits_range"], [15, 15])
        self.assertEqual(preview["expected_credits"], 15)
        self.assertEqual(preview["expected_billable_seconds"], 11.4)

    def test_credit_range_multiplies_by_task_count(self) -> None:
        preview = BudgetPreview().preview(
            cases=[{"generation_mode": "offline"}] * 3,
            skipped=[],
            repeat_count=1,
        )
        self.assertEqual(preview["expected_credits_range"], [240, 450])

    def test_preview_has_no_side_effects(self) -> None:
        before = self.repository.list_assets()
        preview = self.builder.preview(self.request(seed=11))
        after = self.repository.list_assets()
        self.assertEqual(before, after)
        self.assertIn("combination_count", preview)
        self.assertIn("expected_credits_range", preview)
        self.assertIn("per_mode_tasks", preview)
        plans = self.repository.list_batch_manifests()
        self.assertEqual(plans, [])

    def test_missing_prompt_video_is_skipped_with_reason(self) -> None:
        # A video-reference recipe prompt without a prompt video asset.
        self.repository.upsert_asset(
            {
                "asset_id": "prompt-4",
                "kind": "prompt_text",
                "uri": "artifact://assets/prompt-4/source.bin",
                "sha256": "sha-prompt-4",
                "bytes": 10,
                "status": "ready",
                "metadata": {"text": "运镜：按参考视频运镜"},
            }
        )
        plan = self.builder.build(self.request(seed=13))
        skipped = plan["metadata"]["skipped"]
        reasons = [item["reason"] for item in skipped]
        self.assertTrue(any("requires a prompt video" in reason for reason in reasons))


class SchemaTests(PlanningTestBase):
    def test_plan_passes_schema(self) -> None:
        plan = self.builder.build(self.request(seed=17))
        schema = _load_schema("test-plan.schema.json")
        Draft202012Validator(schema).validate(plan)


if __name__ == "__main__":
    unittest.main()
