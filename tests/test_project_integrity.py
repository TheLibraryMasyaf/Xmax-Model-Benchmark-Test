import json
import re
import unittest
from pathlib import Path

from xmax_test.benchmark import load_benchmark_contract
from xmax_test.config import load_config

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
PIPELINE_STAGES = [
    "ingest",
    "plan",
    "generate",
    "preprocess",
    "evaluate",
    "feedback",
    "report",
    "sync",
    "reconcile",
]


class ProjectIntegrityTests(unittest.TestCase):
    def test_p1_validity_has_visual_mlmm_route(self) -> None:
        benchmark = load_benchmark_contract(ROOT / "BENCHMARK.md")
        performance = next(item for item in benchmark["dimensions"] if item["dimension_id"] == "P")
        self.assertIn("mlmm", performance["judge_routing"]["secondary_kinds"])
        registry = json.loads((ROOT / "config" / "judges.example.json").read_text())
        mlmm = next(item for item in registry["judges"] if item["kind"] == "mlmm")
        self.assertIn("P.1", mlmm["supported_criteria"])

    def test_all_json_files_parse(self) -> None:
        paths = sorted((ROOT / "config").glob("*.json")) + sorted((ROOT / "schemas").glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                json.loads(path.read_text(encoding="utf-8"))

    def test_example_schema_paths_exist(self) -> None:
        for path in sorted((ROOT / "config").glob("*.example.json")):
            with self.subTest(path=path.relative_to(ROOT)):
                data = json.loads(path.read_text(encoding="utf-8"))
                schema_ref = data.get("$schema")
                self.assertIsInstance(schema_ref, str)
                self.assertTrue((path.parent / schema_ref).resolve().is_file())

    def test_all_schema_declaring_configs_validate(self) -> None:
        for path in sorted((ROOT / "config").glob("*.json")):
            with self.subTest(path=path.relative_to(ROOT)):
                data = json.loads(path.read_text(encoding="utf-8"))
                schema_ref = data.get("$schema")
                if not schema_ref:
                    continue
                load_config(path, Path(schema_ref).name, base_dir=ROOT)

    def test_local_markdown_links_exist(self) -> None:
        markdown_paths = [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "RUNBOOK.md"]
        markdown_paths += sorted((ROOT / "docs").glob("*.md"))
        markdown_paths += sorted((ROOT / "report-templates").glob("*.md"))
        failures: list[str] = []
        for path in markdown_paths:
            text = path.read_text(encoding="utf-8")
            for target in MARKDOWN_LINK.findall(text):
                if target.startswith(("http://", "https://", "#")):
                    continue
                clean = target.split("#", 1)[0].strip("<>")
                if clean and not (path.parent / clean).resolve().exists():
                    failures.append(f"{path.relative_to(ROOT)} -> {target}")
        self.assertEqual(failures, [])

    def test_model_update_template_keeps_three_level_summary(self) -> None:
        template = (ROOT / "report-templates" / "model-version-update-report.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("### P0 — 新模型明显改进", template)
        self.assertIn("### P1 — 新模型持平", template)
        self.assertIn("### P2 — 新模型劣化", template)
        self.assertIn("## 4. 全量维度变化", template)
        self.assertIn("## 5. 全量细则变化", template)
        self.assertNotIn("Canonical Score", template)
        self.assertIn("{{ report_status }}", template)
        self.assertIn("{{ release_recommendation }}", template)
        self.assertIn("关键数据→变化/问题说明→行动与验收", template)
        self.assertIn("基线/新版标准差（百分点）", template)
        self.assertIn("## 3. 分场景结果", template)

    def test_single_version_template_keeps_scores_and_priority_levels(self) -> None:
        template = (ROOT / "report-templates" / "single-version-evaluation-report.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("## 2. 总分与总体分布", template)
        self.assertIn("## 4. 全量评分维度得分", template)
        self.assertIn("## 5. 评分细则得分", template)
        self.assertIn("### 3.2 表现较好的维度", template)
        self.assertIn("### 3.3 表现不足的维度", template)
        self.assertIn("### P0 — 发布/可用性阻断", template)
        self.assertIn("### P1 — 明显短板", template)
        self.assertIn("### P2 — 局部优化", template)
        self.assertEqual(
            template.count("| 优先问题 | 关键数据 | 问题说明 | 行动与验收 |"),
            3,
        )
        self.assertIn("禁止把维度分平均拆给各细则", template)

    def test_draft_scenario_pack_matches_benchmark_rules(self) -> None:
        scenarios = json.loads((ROOT / "config" / "scenarios.json").read_text(encoding="utf-8"))
        self.assertEqual(scenarios["status"], "shadow")
        self.assertEqual(len(scenarios["scenarios"]), 12)
        self.assertEqual(sum(item["tier"] == "core" for item in scenarios["scenarios"]), 12)
        self.assertEqual(sum(item["tier"] == "supplementary" for item in scenarios["scenarios"]), 0)

    def test_operation_recipes_are_unambiguous(self) -> None:
        recipes = json.loads(
            (ROOT / "config" / "operation-recipes.json").read_text(encoding="utf-8")
        )
        self.assertTrue(recipes["recipe_pack_id"])
        self.assertTrue(recipes["version"])
        recipe_ids = [item["recipe_id"] for item in recipes["recipes"]]
        self.assertEqual(len(recipe_ids), len(set(recipe_ids)))
        for recipe in recipes["recipes"]:
            with self.subTest(recipe_id=recipe["recipe_id"]):
                self.assertIn(
                    recipe["default_generation_mode"],
                    recipe["allowed_generation_modes"],
                )
                self.assertEqual(
                    recipe["expected_audio_source_role"],
                    recipe["edited_video_role"],
                )
                for mode in recipe["allowed_generation_modes"]:
                    self.assertIn(mode, recipe["bindings"])

    def test_feishu_case_score_contract_is_percentage_and_nullable(self) -> None:
        config = json.loads((ROOT / "config" / "feishu.example.json").read_text(encoding="utf-8"))
        self.assertEqual(config["tables"]["case_data"], "tblohc666GKQCi1A")
        case_fields = config["field_projection"]["case_data"]
        self.assertEqual(case_fields["score_percent"], "case评分")
        score = config["case_score"]
        self.assertEqual(score["display"], "percentage")
        self.assertEqual(score["internal_range"], [0, 100])
        self.assertEqual(score["feishu_storage_range"], [0, 1])
        self.assertEqual(score["write_transform"], "divide_by_100")
        self.assertEqual(score["read_transform"], "multiply_by_100")
        self.assertIsNone(score["unscored_value"])
        self.assertEqual(score["failed_run_value"], 0)

        evaluation_schema = json.loads(
            (ROOT / "schemas" / "evaluation-result.schema.json").read_text(encoding="utf-8")
        )
        score_schema = evaluation_schema["properties"]["case_score_percent"]
        self.assertIn("null", score_schema["type"])
        self.assertEqual(score_schema["minimum"], 0)
        self.assertEqual(score_schema["maximum"], 100)

    def test_feishu_sync_schema_exposes_business_tables(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "feishu-record.schema.json").read_text(encoding="utf-8")
        )
        entity_types = schema["properties"]["entity_type"]["enum"]
        self.assertTrue({"feed_data", "prompt_data", "case_data"} <= set(entity_types))

    def test_pipeline_stages_and_default_handoff_are_canonical(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "run-request.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["properties"]["stages"]["items"]["enum"], PIPELINE_STAGES)
        self.assertEqual(schema["properties"]["dependency_policy"]["const"], "explicit_only")
        self.assertEqual(schema["properties"]["missing_input_policy"]["const"], "error")

        example = json.loads(
            (ROOT / "config" / "run-request.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            example["stages"],
            [
                "ingest",
                "plan",
                "generate",
                "preprocess",
                "evaluate",
                "report",
                "sync",
                "reconcile",
            ],
        )
        self.assertEqual(example["dependency_policy"], "explicit_only")
        self.assertEqual(example["missing_input_policy"], "error")
        self.assertEqual(example["sync_policy"], "full")

    def test_stage_manifest_has_versioned_inputs_and_outputs(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "stage-manifest.schema.json").read_text(encoding="utf-8")
        )
        required = set(schema["required"])
        self.assertTrue(
            {
                "stage_run_id",
                "stage",
                "input_refs",
                "output_refs",
                "input_hash",
                "config_hash",
                "producer_version",
            }
            <= required
        )
        self.assertEqual(schema["properties"]["stage"]["enum"], PIPELINE_STAGES)

        batch_schema = json.loads(
            (ROOT / "schemas" / "batch-manifest.schema.json").read_text(encoding="utf-8")
        )
        self.assertTrue(
            {
                "batch_id",
                "entity_type",
                "item_ids",
                "content_hash",
                "producer_stage_run_id",
            }
            <= set(batch_schema["required"])
        )

        selector_schema = json.loads(
            (ROOT / "schemas" / "pipeline-selector.schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("state", selector_schema["required"])
        self.assertEqual(selector_schema["properties"]["state"]["enum"], ["request", "frozen"])

    def test_existing_result_import_is_read_only_and_creates_completed_runs(
        self,
    ) -> None:
        config = json.loads(
            (ROOT / "config" / "existing-results.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config["source"]["kind"], "feishu_case_data")
        self.assertEqual(config["selector"]["entity_type"], "source_record")
        self.assertEqual(config["mode_resolution"]["on_unresolved"], "error")
        self.assertEqual(config["imported_run_status"], "completed")
        self.assertFalse(config["write_remote"])

        run_schema = json.loads(
            (ROOT / "schemas" / "generation-run.schema.json").read_text(encoding="utf-8")
        )
        self.assertTrue({"run_batch_id", "origin", "provenance"} <= set(run_schema["required"]))
        self.assertTrue(
            {"feishu_import", "local_import", "stage_manifest_import"}
            <= set(run_schema["properties"]["origin"]["enum"])
        )

    def test_stage_document_forbids_implicit_generation_and_sync(self) -> None:
        text = (ROOT / "docs" / "stage-orchestration.md").read_text(encoding="utf-8")
        self.assertIn("不得为了补输入而静默增加阶段", text)
        self.assertIn("导入本身永不写远端", text)
        self.assertIn("score_only", text)
        operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
        self.assertIn("完成条件只适用于Run Request显式授权的阶段", operations)

    def test_standalone_run_templates_do_not_expand_scope(self) -> None:
        expected = {
            "run-generate-only.example.json": (["generate"], "none"),
            "run-evaluate-only.example.json": (["preprocess", "evaluate"], "none"),
            "run-import-evaluate-only.example.json": (
                ["ingest", "preprocess", "evaluate"],
                "none",
            ),
            "run-sync-scores-only.example.json": (["sync"], "score_only"),
        }
        for filename, (stages, sync_policy) in expected.items():
            with self.subTest(filename=filename):
                request = json.loads((ROOT / "config" / filename).read_text(encoding="utf-8"))
                self.assertEqual(request["stages"], stages)
                self.assertEqual(request["sync_policy"], sync_policy)
                self.assertEqual(request["dependency_policy"], "explicit_only")
                self.assertEqual(request["missing_input_policy"], "error")


if __name__ == "__main__":
    unittest.main()
