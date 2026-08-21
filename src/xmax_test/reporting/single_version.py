"""Exact-batch single-version report generation."""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..errors import ContractError
from ..evaluation.aggregation import aggregate_evaluation_results
from ..feedback.overrides import HumanOverrideService
from ..hashing import content_hash
from ..time import utc_now


class SingleVersionReportService:
    """Render one model version from one Run Batch and Evaluation Batch.

    The two manifests are mandatory. This prevents a report from silently
    mixing retries, historical benchmark versions, or another agent's batch.
    """

    def __init__(
        self,
        repository: Any,
        benchmark: dict[str, Any],
        scenario_pack: dict[str, Any],
    ) -> None:
        self._repository = repository
        self._benchmark = benchmark
        self._scenario_pack = scenario_pack
        self._overrides = HumanOverrideService(repository)

    def generate(
        self,
        *,
        report_id: str,
        model_version: str,
        run_batch_id: str,
        evaluation_batch_id: str,
        output_directory: str | Path,
        requested_scene_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        run_manifest = self._repository.get_batch_manifest("run_batch", run_batch_id)
        evaluation_manifest = self._repository.get_batch_manifest(
            "evaluation_batch", evaluation_batch_id
        )
        evaluation_ids = set(evaluation_manifest.get("item_ids", []))
        evaluations = {
            item["run_id"]: self._overrides.effective_result(item)
            for item in self._repository.list_evaluation_results(
                evaluation_batch_id=evaluation_batch_id
            )
            if item.get("evaluation_id") in evaluation_ids
        }
        runs = [self._repository.get_run(run_id) for run_id in run_manifest.get("item_ids", [])]
        wrong_models = sorted(
            {str(run.get("model_id")) for run in runs if run.get("model_id") != model_version}
        )
        if wrong_models:
            raise ContractError(
                f"run batch {run_batch_id} contains models other than {model_version}: "
                + ", ".join(wrong_models)
            )
        completed_ids = {run["run_id"] for run in runs if run.get("status") == "completed"}
        extra_evaluations = sorted(set(evaluations) - completed_ids)
        if extra_evaluations:
            raise ContractError(
                "evaluation batch contains runs outside the selected completed run batch: "
                + ", ".join(extra_evaluations)
            )

        effective_results = list(evaluations.values())
        aggregate = aggregate_evaluation_results(effective_results)
        cases = [self._case_row(run, evaluations.get(run["run_id"])) for run in runs]
        scenes = sorted({row["scenario_id"] for row in cases if row.get("scenario_id")})
        requested = requested_scene_ids or scenes
        missing_scenes = sorted(set(requested) - set(scenes))
        total_scores = [
            float(row["score_percent"])
            for row in cases
            if isinstance(row.get("score_percent"), (int, float))
        ]
        # Generation failures participate in every headline score as 0%. A
        # completed-but-not-evaluated run remains missing instead of being
        # silently converted to zero.
        canonical_scores = [
            float(row["canonical_score"])
            for row in cases
            if isinstance(row.get("canonical_score"), (int, float))
        ]
        scenario_scores = [
            float(row["scenario_score"])
            for row in cases
            if isinstance(row.get("scenario_score"), (int, float))
        ]
        dimensions = aggregate["dimension_summary"]
        criteria = aggregate["criterion_summary"]
        priorities = _priorities(cases, dimensions, criteria)
        report = {
            "report_schema_version": "single-version-report/1.0",
            "report_id": report_id,
            "status": (
                "complete"
                if not missing_scenes and len(evaluations) == len(completed_ids)
                else "partial"
            ),
            "generated_at": utc_now(),
            "model_version": model_version,
            "run_batch_id": run_batch_id,
            "evaluation_batch_id": evaluation_batch_id,
            "benchmark_version": self._benchmark.get("benchmark_version", ""),
            "scenario_pack_version": self._scenario_pack.get("version", ""),
            "requested_scene_ids": requested,
            "missing_scene_ids": missing_scenes,
            "coverage": {
                "run_count": len(runs),
                "completed_run_count": len(completed_ids),
                "generation_failure_count": sum(run.get("status") != "completed" for run in runs),
                "evaluated_completed_run_count": len(evaluations),
                "human_override_count": sum(
                    bool(item.get("human_override")) for item in effective_results
                ),
            },
            "scores": {
                "case_score_percent": _stats(total_scores),
                "canonical_score": _stats(canonical_scores),
                "scenario_score": _stats(scenario_scores),
            },
            "dimension_results": dimensions,
            "criterion_results": criteria,
            "case_results": cases,
            "strengths": _rank(dimensions, reverse=True),
            "weaknesses": _rank(dimensions, reverse=False),
            "priorities": priorities,
            "audit": {
                "run_manifest_hash": run_manifest.get("content_hash"),
                "evaluation_manifest_hash": evaluation_manifest.get("content_hash"),
                "benchmark_hash": content_hash(self._benchmark),
                "scenario_pack_hash": content_hash(self._scenario_pack),
                "score_source": "effective criterion_results; generation errors are 0%",
            },
        }
        output = Path(output_directory)
        output.mkdir(parents=True, exist_ok=True)
        schema_path = (
            Path(__file__).resolve().parents[3] / "schemas" / "single-version-report.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        errors = sorted(
            Draft202012Validator(schema).iter_errors(report),
            key=lambda error: list(error.path),
        )
        if errors:
            raise ContractError(f"single-version report schema error: {errors[0].message}")
        json_path = output / f"{report_id}.json"
        markdown_path = output / f"{report_id}.md"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        markdown_path.write_text(_render(report), encoding="utf-8")
        return {
            "report_id": report_id,
            "status": report["status"],
            "markdown_path": str(markdown_path),
            "json_path": str(json_path),
        }

    def _case_row(self, run: dict[str, Any], evaluation: dict[str, Any] | None) -> dict[str, Any]:
        case = self._repository.get_test_case(run["case_id"])
        failed = run.get("status") != "completed"
        score = (
            0.0
            if failed
            else (
                evaluation.get("effective_case_score_percent") if evaluation is not None else None
            )
        )
        return {
            "run_id": run["run_id"],
            "case_number": run.get("case_number", ""),
            "status": run.get("status"),
            "mode": run.get("mode"),
            "scenario_id": case.get("scenario_id"),
            "repeat_index": case.get("repeat_index"),
            "score_percent": score,
            "canonical_score": 0.0
            if failed
            else evaluation.get("canonical_score")
            if evaluation
            else None,
            "scenario_score": score,
            "evaluation_id": evaluation.get("evaluation_id") if evaluation else None,
            "gate_ids": evaluation.get("applied_gate_ids", []) if evaluation else [],
            "human_override": evaluation.get("human_override") if evaluation else None,
            "result_asset_id": run.get("result_asset_id"),
        }


def _rank(items: list[dict[str, Any]], *, reverse: bool) -> list[dict[str, Any]]:
    scored = [item for item in items if isinstance(item.get("score_percent"), (int, float))]
    return sorted(
        scored,
        key=lambda item: (float(item["score_percent"]), item.get("dimension_id", "")),
        reverse=reverse,
    )[:5]


def _priorities(
    cases: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    criteria: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    p0 = []
    failed = [item for item in cases if item.get("score_percent") == 0]
    gated = [item for item in cases if item.get("gate_ids")]
    if failed or gated:
        p0.append(
            {
                "issue": "generation failure, zero-score result, or hard-gate failure",
                "affected_runs": sorted({item["run_id"] for item in failed + gated}),
                "recommendation": "inspect the bound artifacts and rerun the same cases after the root cause is fixed",
            }
        )
    scored_criteria = sorted(
        (item for item in criteria if isinstance(item.get("score_percent"), (int, float))),
        key=lambda item: (float(item["score_percent"]), item.get("criterion_id", "")),
    )
    # BENCHMARK does not publish an absolute "bad" threshold. Only produce
    # relative improvement priorities when the batch actually has a score
    # spread; never invent a 60% pass line in report code.
    weak_criteria = (
        scored_criteria[: min(10, len(scored_criteria))]
        if scored_criteria
        and float(scored_criteria[0]["score_percent"]) < float(scored_criteria[-1]["score_percent"])
        else []
    )
    p1 = [
        {
            "issue": f"criterion {item['criterion_id']} is relatively low in this batch",
            "affected_criterion": item["criterion_id"],
            "score_percent": item["score_percent"],
            "recommendation": "build a targeted replay set around the observed criterion and verify against its Benchmark anchors",
        }
        for item in weak_criteria[:10]
    ]
    variable_dimensions = sorted(
        (
            item
            for item in dimensions
            if isinstance(item.get("standard_deviation"), (int, float))
            and float(item["standard_deviation"]) > 0
        ),
        key=lambda item: (
            -float(item["standard_deviation"]),
            item.get("dimension_id", ""),
        ),
    )
    p2 = [
        {
            "issue": f"dimension {item['dimension_id']} has high variance",
            "affected_dimension": item["dimension_id"],
            "standard_deviation": item.get("standard_deviation"),
            "recommendation": "increase repeats for this dimension's representative scenarios",
        }
        for item in variable_dimensions[:10]
    ]
    return {"p0": p0, "p1": p1, "p2": p2}


def _stats(values: Iterable[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {
            key: None
            for key in (
                "mean",
                "median",
                "minimum",
                "maximum",
                "standard_deviation",
                "p25",
                "p75",
            )
        } | {"count": 0}
    return {
        "count": len(ordered),
        "mean": round(statistics.mean(ordered), 2),
        "median": round(statistics.median(ordered), 2),
        "minimum": round(min(ordered), 2),
        "maximum": round(max(ordered), 2),
        "standard_deviation": round(statistics.pstdev(ordered), 2),
        "p25": round(_percentile(ordered, 0.25), 2),
        "p75": round(_percentile(ordered, 0.75), 2),
    }


def _percentile(values: list[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _render(report: dict[str, Any]) -> str:
    coverage = report["coverage"]
    scores = report["scores"]
    lines = [
        f"# XMAX single-version evaluation: {report['model_version']}",
        "",
        f"- Report: `{report['report_id']}`",
        f"- Status: `{report['status']}`",
        f"- Run batch: `{report['run_batch_id']}`",
        f"- Evaluation batch: `{report['evaluation_batch_id']}`",
        f"- Benchmark: `{report['benchmark_version']}`",
        "",
        "## Summary",
        "",
        f"- Case score: {_format_stats(scores['case_score_percent'])}",
        f"- Canonical score: {_format_stats(scores['canonical_score'])}",
        f"- Scenario score: {_format_stats(scores['scenario_score'])}",
        f"- Runs: {coverage['run_count']}; completed: {coverage['completed_run_count']}; generation failures: {coverage['generation_failure_count']}; evaluated: {coverage['evaluated_completed_run_count']}; human overrides: {coverage['human_override_count']}",
        "",
        "## Dimensions",
        "",
        "| Dimension | Score | Coverage | Std dev |",
        "| --- | ---: | ---: | ---: |",
    ]
    for item in report["dimension_results"]:
        lines.append(
            f"| {item['dimension_id']} | {_pct(item.get('score_percent'))} | {item.get('assessable_count', 0)}/{item.get('total_evaluations', 0)} | {_value(item.get('standard_deviation'))} |"
        )
    lines.extend(
        [
            "",
            "## Criteria",
            "",
            "| Dimension | Criterion | Score | Coverage | Distribution 0/(0,1)/1/(1,2)/2 |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for item in report["criterion_results"]:
        distribution = item.get("score_distribution") or {}
        values = "/".join(
            str(distribution.get(key, 0)) for key in ("0", "0_to_1", "1", "1_to_2", "2")
        )
        lines.append(
            f"| {item.get('dimension_id', '')} | {item['criterion_id']} {item.get('criterion_name', '')} | {_pct(item.get('score_percent'))} | {item.get('assessable_count', 0)}/{item.get('total_evaluations', 0)} | {values} |"
        )
    for priority in ("p0", "p1", "p2"):
        lines.extend(["", f"## {priority.upper()} recommendations", ""])
        items = report["priorities"][priority]
        if not items:
            lines.append("- None supported by this batch.")
        else:
            for item in items:
                lines.append(f"- {item['issue']}. Recommendation: {item['recommendation']}")
    lines.extend(
        [
            "",
            "## Per-case results",
            "",
            "| Case | Run | Status | Scene | Repeat | Score | Evaluation |",
            "| --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for item in report["case_results"]:
        lines.append(
            f"| {item['case_number']} | {item['run_id']} | {item['status']} | {item.get('scenario_id') or ''} | {item.get('repeat_index') or ''} | {_pct(item.get('score_percent'))} | {item.get('evaluation_id') or 'not evaluated'} |"
        )
    lines.extend(
        [
            "",
            "## Audit",
            "",
            "```json",
            json.dumps(report["audit"], ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _format_stats(stats: dict[str, Any]) -> str:
    if not stats.get("count"):
        return "not assessable"
    return (
        f"mean {_pct(stats['mean'])}, median {_pct(stats['median'])}, "
        f"min/max {_pct(stats['minimum'])}/{_pct(stats['maximum'])}, "
        f"std {_value(stats['standard_deviation'])}, n={stats['count']}"
    )


def _pct(value: Any) -> str:
    return "N/A" if not isinstance(value, (int, float)) else f"{float(value):.2f}%"


def _value(value: Any) -> str:
    return "N/A" if not isinstance(value, (int, float)) else f"{float(value):.4f}"
