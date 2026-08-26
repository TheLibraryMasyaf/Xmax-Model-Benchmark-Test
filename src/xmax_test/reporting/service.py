"""Model-update report orchestration.

Pipeline: compare -> classify -> build JSON -> render Markdown -> write both.
Rejects non-comparable up/down conclusions, missing template placeholders and
missing requested scenes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .classification import bucket_pairs, classify_delta, comparison_policy
from .comparison import ModelComparisonService
from .renderer import build_report_json, render_markdown, template_hash, write_report
from .repository import ReportRepository


class ModelUpdateReportService:
    def __init__(
        self,
        repository: ReportRepository,
        comparison: ModelComparisonService,
        benchmark: dict[str, Any],
        scenario_pack: dict[str, Any],
        score_schema: dict[str, Any],
    ) -> None:
        self._repository = repository
        self._comparison = comparison
        self._benchmark = benchmark
        self._scenario_pack = scenario_pack
        self._score_schema = score_schema

    def generate(
        self,
        *,
        comparison_id: str,
        baseline_model_version: str,
        candidate_model_version: str,
        baseline_run_batch_id: str,
        candidate_run_batch_id: str,
        baseline_evaluation_batch_id: str,
        candidate_evaluation_batch_id: str,
        requested_scene_ids: list[str],
        template_path: str | Path,
        output_directory: str | Path,
    ) -> dict[str, Any]:
        compared = self._comparison.compare(
            baseline_model_version=baseline_model_version,
            candidate_model_version=candidate_model_version,
            baseline_run_batch_id=baseline_run_batch_id,
            candidate_run_batch_id=candidate_run_batch_id,
            baseline_evaluation_batch_id=baseline_evaluation_batch_id,
            candidate_evaluation_batch_id=candidate_evaluation_batch_id,
            requested_scene_ids=requested_scene_ids,
        )
        status = compared.get("status", "not_comparable")
        pairs = compared.get("pairs", [])

        policy = comparison_policy(self._score_schema)
        classified = (
            bucket_pairs(pairs, policy)
            if pairs
            else {"p0": [], "p1": [], "p2": [], "unclassified": []}
        )
        if status == "not_comparable":
            classified = {"p0": [], "p1": [], "p2": [], "unclassified": []}
        for score_name in ("canonical", "scenario"):
            score = compared.get("overall", {}).get(score_name)
            if isinstance(score, dict):
                score["classification"] = (
                    "not_comparable"
                    if status == "not_comparable"
                    else classify_delta(score.get("delta_points"), policy)
                )
        classified = {
            key: [_to_comparison_item(item) for item in items] for key, items in classified.items()
        }
        for criterion in compared.get("overall", {}).get("criteria", []):
            criterion["classification"] = classify_delta(
                criterion.get("delta_points"), policy
            )
        for scene in compared.get("scene_results", []):
            scene["score_summary"]["classification"] = classify_delta(
                scene.get("score_summary", {}).get("delta_points"), policy
            )
            for dimension in scene.get("dimensions", []):
                dimension["classification"] = classify_delta(
                    dimension.get("delta_points"), policy
                )
        for dimension in compared.get("overall", {}).get("dimensions", []):
            bucket = classify_delta(dimension.get("delta_points"), policy)
            dimension["classification"] = bucket
            classified.setdefault(bucket, []).append(
                {
                    "scope": "dimension",
                    "item_id": dimension["dimension_id"],
                    "item_name": dimension.get("dimension_name", ""),
                    "scenario_id": None,
                    "baseline": dimension.get("baseline"),
                    "candidate": dimension.get("candidate"),
                    "delta_points": dimension.get("delta_points"),
                    "severity": "medium",
                    "evidence_ids": [],
                }
            )

        report = build_report_json(
            comparison_id=comparison_id,
            status=status,
            baseline_model_version=baseline_model_version,
            candidate_model_version=candidate_model_version,
            benchmark_version=self._benchmark.get("benchmark_version", ""),
            score_schema_version=self._score_schema.get("version", ""),
            scenario_pack_version=self._scenario_pack.get("version", ""),
            requested_scene_ids=requested_scene_ids,
            comparison=compared,
            classified=classified,
            template_path=str(template_path),
            template_hash_value="",
            artifact_uris=[],
        )
        evaluation_ids = {
            pair[side].get("_evaluation", {}).get("evaluation_id")
            for pair in pairs
            for side in ("baseline", "candidate")
        }
        evaluation_ids.discard(None)
        related_signals = [
            item
            for item in self._repository.human_signals()
            if item.get("evaluation_id") in evaluation_ids
        ]
        related_overrides = [
            item
            for item in self._repository.evaluation_overrides()
            if item.get("evaluation_id") in evaluation_ids
        ]
        report["human_signal_summary"] = {
            "count": len(related_signals),
            "overrides": len(related_overrides),
            "signal_ids": sorted(item.get("signal_id", "") for item in related_signals),
            "override_ids": sorted(item.get("override_id", "") for item in related_overrides),
        }

        present_scenes = {scene["scenario_id"] for scene in compared.get("scene_results", [])}
        missing_scenes = [scene for scene in requested_scene_ids if scene not in present_scenes]
        if missing_scenes:
            if report["status"] != "not_comparable":
                report["status"] = "partial"
                report["release_recommendation"] = "retest"
            report["comparability"]["excluded_case_ids"] = (
                report["comparability"].get("excluded_case_ids", []) + missing_scenes
            )

        template_text = Path(template_path).read_text(encoding="utf-8")
        report["audit"]["template_hash"] = template_hash(template_text)
        report["audit"]["artifact_uris"] = [
            str(Path(output_directory) / f"{comparison_id}.md"),
            str(Path(output_directory) / f"{comparison_id}.json"),
        ]
        markdown = render_markdown(report, template_text)
        written = write_report(report, markdown, output_directory, template_text=template_text)
        return {
            "comparison_id": comparison_id,
            "status": report["status"],
            "comparability": report["comparability"].get("comparable"),
            "p0_count": len(report["p0_improvements"]),
            "p1_count": len(report["p1_ties"]),
            "p2_count": len(report["p2_regressions"]),
            **written,
        }


def _to_comparison_item(classified: dict[str, Any]) -> dict[str, Any]:
    scenario = classified.get("scenario", {})
    return {
        "scope": "scene" if classified.get("scenario_id") else "overall",
        "item_id": classified.get("case_number", ""),
        "scenario_id": classified.get("scenario_id"),
        "baseline": scenario.get("baseline"),
        "candidate": scenario.get("candidate"),
        "delta_points": scenario.get("delta_points"),
        "severity": "blocker" if classified.get("new_hard_gate_failure") else "medium",
        "evidence_ids": classified.get("evidence_ids", []),
    }
