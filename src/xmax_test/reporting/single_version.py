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
        runs = sorted(
            [self._repository.get_run(run_id) for run_id in run_manifest.get("item_ids", [])],
            key=lambda item: _natural_key(
                str(item.get("case_number") or item.get("run_id") or "")
            ),
        )
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
        dimensions = sorted(
            aggregate["dimension_summary"],
            key=lambda item: _natural_key(item.get("dimension_id", "")),
        )
        criteria = sorted(
            aggregate["criterion_summary"],
            key=lambda item: _natural_key(item.get("criterion_id", "")),
        )
        priorities = _evidence_packets(cases, dimensions, criteria)
        credibility = _credibility(cases, evaluations, len(runs))
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
            "credibility": credibility,
            "analysis_required": True,
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
            "operation_recipe_id": case.get("operation_recipe_id"),
        }


def _rank(items: list[dict[str, Any]], *, reverse: bool) -> list[dict[str, Any]]:
    scored = [item for item in items if isinstance(item.get("score_percent"), (int, float))]
    return sorted(
        scored,
        key=lambda item: (
            float(item["score_percent"]),
            _natural_key(item.get("dimension_id", "")),
        ),
        reverse=reverse,
    )[:5]


def _evidence_packets(
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
                "issue": "存在生成失败、零分结果或硬门槛失败",
                "affected_runs": sorted(
                    {item["run_id"] for item in failed + gated}, key=_natural_key
                ),
                "analysis_status": "requires_agent_analysis",
            }
        )
    scored_criteria = sorted(
        (item for item in criteria if isinstance(item.get("score_percent"), (int, float))),
        key=lambda item: (
            float(item["score_percent"]),
            _natural_key(item.get("criterion_id", "")),
        ),
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
            "issue": f"细则 {item['criterion_id']} 在本批次相对较低",
            "affected_criterion": item["criterion_id"],
            "score_percent": item["score_percent"],
            "analysis_status": "requires_agent_analysis",
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
            _natural_key(item.get("dimension_id", "")),
        ),
    )
    p2 = [
        {
            "issue": f"维度 {item['dimension_id']} 在本批次波动较大",
            "affected_dimension": item["dimension_id"],
            "standard_deviation": item.get("standard_deviation"),
            "analysis_status": "requires_agent_analysis",
        }
        for item in variable_dimensions[:10]
    ]
    return {"p0": p0, "p1": p1, "p2": p2}


def _credibility(
    cases: list[dict[str, Any]],
    evaluations: dict[str, dict[str, Any]],
    run_count: int,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    completed = [item for item in cases if item.get("status") == "completed"]
    if len(evaluations) != len(completed):
        issues.append(
            {
                "code": "incomplete_evaluation_coverage",
                "severity": "error",
                "description": "完成的Run未全部进入所选评测批次。",
            }
        )
    scenarios = {item.get("scenario_id") for item in cases if item.get("scenario_id")}
    recipes = {
        item.get("operation_recipe_id") for item in cases if item.get("operation_recipe_id")
    }
    if run_count >= 10 and len(recipes) > 1 and len(scenarios) <= 1:
        issues.append(
            {
                "code": "scenario_assignment_degenerate",
                "severity": "error",
                "description": "多个玩法配方被压缩到同一个场景，场景权重结果仅可诊断使用。",
            }
        )
    for evaluation in evaluations.values():
        for criterion in evaluation.get("criterion_results", []):
            for raw in criterion.get("raw_metrics", []):
                values = raw.get("values") or {}
                observed = max(
                    int(values.get("run_count") or 0),
                    int(values.get("attempt_count") or 0),
                )
                if observed > run_count:
                    issues.append(
                        {
                            "code": "group_scope_exceeds_manifest",
                            "severity": "error",
                            "description": (
                                f"组级细则 {criterion.get('criterion_id')} 引用了 {observed} 条Run，"
                                f"超过manifest的 {run_count} 条。"
                            ),
                        }
                    )
                    break
    issues.append(
        {
            "code": "automated_judges_only",
            "severity": "warning",
            "description": "本报告未包含独立人工盲评校准，结论应结合抽样复核。",
        }
    )
    unique = {(item["code"], item["description"]): item for item in issues}
    issues = list(unique.values())
    return {
        "status": (
            "diagnostic"
            if any(item["severity"] == "error" for item in issues)
            else "credible_with_warnings"
        ),
        "issues": issues,
    }


def _natural_key(value: Any) -> tuple[Any, ...]:
    import re

    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", str(value))
    )


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
        f"# XMAX {report['model_version']} 单版本评测报告",
        "",
        f"- 报告ID：`{report['report_id']}`",
        f"- 完整性状态：`{report['status']}`",
        f"- 可信度状态：`{report['credibility']['status']}`",
        f"- Run批次：`{report['run_batch_id']}`",
        f"- 评测批次：`{report['evaluation_batch_id']}`",
        f"- Benchmark：`{report['benchmark_version']}`",
        "",
        "## 摘要",
        "",
        f"- Case分：{_format_stats(scores['case_score_percent'])}",
        f"- 通用分：{_format_stats(scores['canonical_score'])}",
        f"- 场景分：{_format_stats(scores['scenario_score'])}",
        f"- Run总数：{coverage['run_count']}；完成：{coverage['completed_run_count']}；生成失败：{coverage['generation_failure_count']}；已评测：{coverage['evaluated_completed_run_count']}；人工修订：{coverage['human_override_count']}",
        "",
        "## 可信度检查",
        "",
        *[
            f"- [{item['severity']}] {item['description']}"
            for item in report["credibility"]["issues"]
        ],
        "",
        "## 维度结果",
        "",
        "| 维度 | 分数 | 覆盖 | 标准差 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for item in report["dimension_results"]:
        lines.append(
            f"| {item['dimension_id']} | {_pct(item.get('score_percent'))} | {item.get('assessable_count', 0)}/{item.get('total_evaluations', 0)} | {_value(item.get('standard_deviation'))} |"
        )
    lines.extend(
        [
            "",
            "## 细则结果",
            "",
            "| 维度 | 细则 | 分数 | 覆盖 | 分布 0/(0,1)/1/(1,2)/2 |",
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
        lines.extend(["", f"## {priority.upper()} 证据包（待Agent分析）", ""])
        items = report["priorities"][priority]
        if not items:
            lines.append("- 本批次没有形成该级别的证据包。")
        else:
            for item in items:
                affected = (
                    item.get("affected_criterion")
                    or item.get("affected_dimension")
                    or ", ".join(item.get("affected_runs", []))
                )
                lines.append(f"- {item['issue']}。受影响对象：{affected}")
    lines.extend(
        [
            "",
            "## 逐Case结果",
            "",
            "| Case | Run | 状态 | 场景 | 重复序号 | 分数 | Evaluation |",
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
            "## 审计信息",
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
        return "不可评"
    return (
        f"均值 {_pct(stats['mean'])}，中位数 {_pct(stats['median'])}，"
        f"最小/最大 {_pct(stats['minimum'])}/{_pct(stats['maximum'])}，"
        f"标准差 {_value(stats['standard_deviation'])}，n={stats['count']}"
    )


def _pct(value: Any) -> str:
    return "不可评" if not isinstance(value, (int, float)) else f"{float(value):.2f}%"


def _value(value: Any) -> str:
    return "不可评" if not isinstance(value, (int, float)) else f"{float(value):.4f}"
