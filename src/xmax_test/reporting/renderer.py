"""Report renderer.

Generates the model-version-update JSON (machine source of truth) and the
Markdown from the fixed template. Every template placeholder is either filled
with data or a safe "未取得/不可比较" marker; placeholders the renderer does
not know are rejected. The template hash and reporter version are recorded in
the audit block.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ..errors import ContractError

REPORTER_VERSION = "0.3.0-pger-comparison"
PLACEHOLDER = re.compile(r"\{\{\s*(.*?)\s*\}\}")
UNKNOWN_VALUE = "—"


class ReportRenderError(ContractError):
    code = "xmax.report_placeholder"


def template_hash(template_text: str) -> str:
    return hashlib.sha256(template_text.encode("utf-8")).hexdigest()


def build_report_json(
    *,
    comparison_id: str,
    status: str,
    baseline_model_version: str,
    candidate_model_version: str,
    benchmark_version: str,
    score_schema_version: str,
    scenario_pack_version: str,
    requested_scene_ids: list[str],
    comparison: dict[str, Any],
    classified: dict[str, list[dict[str, Any]]],
    template_path: str,
    template_hash_value: str,
    artifact_uris: list[str],
) -> dict[str, Any]:
    """Assemble the JSON report following model-version-report.schema.json."""

    score_summary = {
        "canonical": _score_delta(comparison.get("overall", {}).get("canonical")),
        "scenario": _score_delta(comparison.get("overall", {}).get("scenario")),
    }
    return {
        "comparison_id": comparison_id,
        "status": status,
        "baseline_model_version": baseline_model_version,
        "candidate_model_version": candidate_model_version,
        "benchmark_version": benchmark_version,
        "score_schema_version": score_schema_version,
        "scenario_pack_version": scenario_pack_version,
        "generation_config_hash": comparison.get("generation_config_hash"),
        "judge_versions": comparison.get("judge_versions", []),
        "requested_scene_ids": requested_scene_ids,
        "comparability": comparison.get("comparability", {"comparable": False, "differences": []}),
        "score_summary": score_summary,
        "distribution_stats": {
            "baseline": comparison.get("overall", {}).get("baseline_stats", {"n": 0}),
            "candidate": comparison.get("overall", {}).get("candidate_stats", {"n": 0}),
        },
        "dimension_results": comparison.get("overall", {}).get("dimensions", []),
        "criterion_results": comparison.get("overall", {}).get("criteria", []),
        "reporting_metric_results": comparison.get(
            "reporting_metrics", {"baseline": {}, "candidate": {}}
        ),
        "p0_improvements": classified.get("p0", []),
        "p1_ties": classified.get("p1", []),
        "p2_regressions": classified.get("p2", []),
        "scene_results": _scene_results_json(comparison.get("scene_results", [])),
        "human_signal_summary": {"count": 0, "overrides": 0},
        "analysis_required": True,
        "release_recommendation": _recommendation(status, classified, comparison),
        "audit": {
            "template_path": template_path,
            "template_hash": template_hash_value,
            "reporter_version": REPORTER_VERSION,
            "artifact_uris": artifact_uris,
        },
    }


def render_markdown(report: dict[str, Any], template_text: str) -> str:
    """Render the report into the fixed template with full substitution."""

    map_ = {
        "comparison_id": report.get("comparison_id", ""),
        "tested_at": _now(),
        "baseline_model_version": report.get("baseline_model_version", ""),
        "candidate_model_version": report.get("candidate_model_version", ""),
        "benchmark_version": report.get("benchmark_version", ""),
        "score_schema_version": report.get("score_schema_version", ""),
        "scenario_pack_version": report.get("scenario_pack_version", ""),
        "judge_versions": _join(report.get("judge_versions") or ["未取得/不可比较"]),
        "plan_hash": report.get("plan_hash") or "未取得/不可比较",
        "generation_config_hash": report.get("generation_config_hash") or "未取得/不可比较",
        "requested_scenes": ", ".join(report.get("requested_scene_ids", [])),
        "report_status": report.get("status", "not_comparable"),
        "release_recommendation": report.get("release_recommendation", "shadow"),
        "comparability_conclusion": _comparability_conclusion(report),
        "top_drivers": report.get("top_drivers") or _bucket_ids(report.get("p0_improvements", [])),
        "improvement_analysis": report.get("improvement_analysis")
        or _bucket_summary("明显改进", report.get("p0_improvements", [])),
        "tie_analysis": report.get("tie_analysis")
        or _bucket_summary("持平", report.get("p1_ties", [])),
        "regression_analysis": report.get("regression_analysis")
        or _bucket_summary("劣化", report.get("p2_regressions", [])),
        "failures_and_skips": report.get("failures_and_skips") or "无",
        "blockers_or_none": report.get("blockers") or "无",
        "follow_up_scenes": report.get("follow_up_scenes") or "无",
        "priorities": report.get("priorities") or "无",
        "accepted_risks_or_none": report.get("accepted_risks") or "无",
        "artifact_uris": ", ".join(report.get("audit", {}).get("artifact_uris", [])),
        "hashes": f"Benchmark {report.get('benchmark_version', '')} / ScenarioPack {report.get('scenario_pack_version', '')}",
        "weight_and_gate_audit": report.get("weight_and_gate_audit") or "见 EvaluationResult",
        "raw_index": report.get("raw_index") or "见 Evaluation/Judgment 产物",
        "reporter_version": report.get("audit", {}).get("reporter_version", REPORTER_VERSION),
        "template_hash": report.get("audit", {}).get("template_hash", ""),
        "提升/下降/持平": _direction(report),
        "details": "0",
        "n": "0",
        "score": _fmt(report.get("score_summary", {}).get("scenario", {}).get("candidate")),
        "delta_points": _fmt(
            report.get("score_summary", {}).get("scenario", {}).get("delta_points")
        ),
        "delta_percent": _fmt(
            report.get("score_summary", {}).get("scenario", {}).get("delta_percent")
        ),
        "conclusion": _direction(report),
        **_distribution_placeholders(report),
        **_reporting_metric_placeholders(report),
    }

    text = template_text
    text = _render_scene_sections(text, report)
    text = _render_score_rows(text, report)
    text = _render_comparability_rows(text, report)
    text = _render_scene_coverage(text, report)
    text = _render_dimension_rows(text, report)
    text = _render_criterion_rows(text, report)
    text = _render_item_rows(text, "p0", report.get("p0_improvements", []))
    text = _render_item_rows(text, "p2", report.get("p2_regressions", []))
    text = _render_item_rows(text, "p1", report.get("p1_ties", []))
    # Fill simple named placeholders (data or "未取得/不可比较").
    text = PLACEHOLDER.sub(
        lambda match: str(map_.get(match.group(1), f"{{{{{match.group(1)}}}}}")), text
    )
    # Generic table-cell markers become the "未取得" dash.
    text = re.sub(r"\{\{\s*\.\.\.\s*\}\}", UNKNOWN_VALUE, text)

    leftovers = sorted({name for name in PLACEHOLDER.findall(text)})
    if leftovers:
        raise ReportRenderError(f"unreplaced report placeholders: {leftovers}")
    return text


def write_report(
    report: dict[str, Any],
    markdown: str,
    output_directory: str | Path,
    *,
    template_text: str,
) -> dict[str, Any]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    comparison_id = report["comparison_id"]
    md_path = output / f"{comparison_id}.md"
    json_path = output / f"{comparison_id}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "template_hash": template_hash(template_text),
        "reporter_version": REPORTER_VERSION,
    }


# ----------------------------------------------------------------------
def _render_scene_sections(text: str, report: dict[str, Any]) -> str:
    scenes = report.get("scene_results", [])
    start_marker = "### 3.x"
    end_marker = "## 4."
    start_index = text.find(start_marker)
    end_index = text.find(end_marker)
    if start_index == -1 or end_index == -1:
        return text
    block = text[start_index:end_index]
    sections: list[str] = []
    for index, scene in enumerate(scenes, start=1):
        item = block.replace("### 3.x", f"### 3.{index}", 1)
        replacements = {
            "{{ scenario_id }}": scene.get("scenario_id", UNKNOWN_VALUE),
            "{{ scenario_name }}": scene.get("scenario_name", UNKNOWN_VALUE),
            "{{ scene_mode }}": scene.get("mode", UNKNOWN_VALUE),
            "{{ comparable_pairs }}": str(scene.get("comparable_pairs", 0)),
            "{{ scene_score_delta }}": _points(
                scene.get("score_summary", {}).get("delta_points")
            )
            + " 个百分点",
            "{{ run_ids / evaluation_ids / artifact_uris }}": (
                ", ".join(scene.get("evidence_ids", [])) or "无"
            ),
            "{{ scene_conclusion }}": _scene_conclusion(scene),
        }
        dimension_rows = [
            f"| `{dimension.get('dimension_id', '')}` {dimension.get('dimension_name', '')} | "
            f"{_pct(dimension.get('baseline'))} | {_pct(dimension.get('candidate'))} | "
            f"{_points(dimension.get('delta_points'))} | "
            f"{dimension.get('baseline_assessable_count', 0)}/"
            f"{dimension.get('candidate_assessable_count', 0)} | "
            f"{dimension.get('classification', 'unclassified')} | {UNKNOWN_VALUE} |"
            for dimension in scene.get("dimensions", [])
        ]
        item = re.sub(
            r"\| `\{\{ dimension \}\}` \|.*?\|\n",
            "\n".join(dimension_rows) + "\n" if dimension_rows else "无可比较维度\n",
            item,
        )
        for placeholder, value in replacements.items():
            item = item.replace(placeholder, value)
        item = re.sub(r"\{\{\s*\.\.\.\s*\}\}", UNKNOWN_VALUE, item)
        sections.append(item)
    return text[:start_index] + "\n".join(sections) + text[end_index:]


def _render_score_rows(text: str, report: dict[str, Any]) -> str:
    summary = report.get("score_summary", {})
    scenario = summary.get("scenario", {})
    scenario_row = _score_row("请求场景加权Scenario Score", scenario)
    pattern = re.compile(
        r"\| 请求场景加权Scenario Score \|.*?\|\n",
        re.DOTALL,
    )
    return pattern.sub(scenario_row + "\n", text)


def _render_scene_coverage(text: str, report: dict[str, Any]) -> str:
    rows = []
    for scene in report.get("scene_results", []):
        rows.append(
            f"| `{scene.get('scenario_id', '')}` | `{scene.get('mode', 'offline')}` | "
            f"`{scene.get('comparable_pairs', 0)}` | `{scene.get('comparable_pairs', 0)}` | "
            f"`{scene.get('comparable_pairs', 0)}` | "
            f"`{_coverage_boundary(scene, report)}` |"
        )
    pattern = re.compile(r"\| `\{\{ scene \}\}`.*?\|\n", re.DOTALL)
    return pattern.sub("\n".join(rows) + "\n" if rows else "", text)


def _render_dimension_rows(text: str, report: dict[str, Any]) -> str:
    rows = [
        f"| `{item.get('dimension_id', '')}` {item.get('dimension_name', '')} | "
        f"{_pct(item.get('baseline'))} | {_pct(item.get('candidate'))} | "
        f"{_points(item.get('delta_points'))} | "
        f"{item.get('baseline_assessable_count', 0)}/"
        f"{item.get('candidate_assessable_count', 0)} | "
        f"{_fmt(item.get('baseline_standard_deviation_points'))}/"
        f"{_fmt(item.get('candidate_standard_deviation_points'))} | "
        f"{item.get('classification', 'unclassified')} | {UNKNOWN_VALUE} |"
        for item in report.get("dimension_results", [])
    ]
    pattern = re.compile(
        r"\| `\{\{ dimension_id \}\}` \{\{ dimension_name \}\}.*?\|\n",
        re.DOTALL,
    )
    return pattern.sub("\n".join(rows) + "\n" if rows else "无可比较维度\n", text)


def _render_criterion_rows(text: str, report: dict[str, Any]) -> str:
    rows = []
    for item in report.get("criterion_results", []):
        rows.append(
            f"| `{item.get('dimension_id', '')}` {item.get('dimension_name', '')} | "
            f"`{item.get('criterion_id', '')}` {item.get('criterion_name') or UNKNOWN_VALUE} | "
            f"{_score_with_status(item.get('baseline_status'), item.get('baseline'))} | "
            f"{_score_with_status(item.get('candidate_status'), item.get('candidate'))} | "
            f"{item.get('baseline_assessable_count', 0)}/"
            f"{item.get('candidate_assessable_count', 0)} | "
            f"{_fmt(item.get('baseline_standard_deviation_points'))}/"
            f"{_fmt(item.get('candidate_standard_deviation_points'))} | "
            f"{_points(item.get('delta_points'))} | "
            f"{item.get('classification', 'unclassified')} | {UNKNOWN_VALUE} |"
        )
    pattern = re.compile(
        r"\| `\{\{ dimension \}\}` \| `\{\{ criterion_id \}\}`.*?\|\n",
        re.DOTALL,
    )
    return pattern.sub("\n".join(rows) + "\n" if rows else "无可比较的细则结果\n", text)


def _render_item_rows(text: str, bucket: str, items: list[dict[str, Any]]) -> str:
    if bucket == "p0":
        pattern = re.compile(r"\| 1 \| `\{\{ item \}\}`.*?\|\n", re.DOTALL)
        rows = [
            f"| {index} | `{_item_label(item)}` | {_pct(item.get('baseline'))} | "
            f"{_pct(item.get('candidate'))} | {_points(item.get('delta_points'))} | "
            f"{_fmt(item.get('evidence_ids', []))} | {UNKNOWN_VALUE} | {UNKNOWN_VALUE} |"
            for index, item in enumerate(items, start=1)
        ]
    elif bucket == "p1":
        pattern = re.compile(r"\| `\{\{ item \}\}`.*?\|\n", re.DOTALL)
        rows = [
            f"| `{_item_label(item)}` | {_pct(item.get('baseline'))} | "
            f"{_pct(item.get('candidate'))} | {_points(item.get('delta_points'))} | "
            f"{_fmt(item.get('evidence_ids', []))} | 持平区间内，无显著变化 | {UNKNOWN_VALUE} |"
            for item in items
        ]
    else:
        pattern = re.compile(r"\| `blocker/high/medium/low` \| `\{\{ item \}\}`.*?\|\n", re.DOTALL)
        rows = [
            f"| {item.get('severity', 'medium')} | `{_item_label(item)}` | "
            f"{_pct(item.get('baseline'))} | {_pct(item.get('candidate'))} | "
            f"{_points(item.get('delta_points'))} | {_fmt(item.get('evidence_ids', []))} | "
            f"{UNKNOWN_VALUE} | {UNKNOWN_VALUE} |"
            for item in items
        ]
    return pattern.sub("\n".join(rows) + "\n" if rows else "无满足条件的项目\n", text)


def _score_row(label: str, delta: dict[str, Any]) -> str:
    return (
        f"| {label} | `{_pct(delta.get('baseline'))}` | `{_pct(delta.get('candidate'))}` | "
        f"`{_points(delta.get('delta_points'))}` | `{_pct(delta.get('delta_percent'))}` | "
        f"`{delta.get('classification', 'unclassified')}` |"
    )


def _comparability_conclusion(report: dict[str, Any]) -> str:
    comparability = report.get("comparability", {})
    if comparability.get("comparable"):
        return "可比，可以计算正式升降结论。"
    differences = comparability.get("differences", [])
    detail = "; ".join(str(d) for d in differences[:5])
    if report.get("status") == "partial":
        return f"部分可比：{detail}；只统计未受影响的配对。"
    return f"不可比：{detail}" if detail else "不可比，仅报告事实。"


def _render_comparability_rows(text: str, report: dict[str, Any]) -> str:
    differences = report.get("comparability", {}).get("differences", [])
    if not differences:
        rows = ["| 全部可比性门槛 | 一致 | 可进入正式升降计算 |"]
    else:
        rows = [
            f"| {_comparability_label(item.get('kind'))} | {_comparability_detail(item)} | "
            "排除受影响配对；其余配对可继续统计 |"
            for item in differences
        ]
    pattern = re.compile(r"\| `\{\{ comparability_item \}\}`.*?\|\n", re.DOTALL)
    return pattern.sub("\n".join(rows) + "\n", text)


def _comparability_label(kind: Any) -> str:
    return {
        "missing_runs": "Run或Evaluation缺失",
        "unpaired_cases": "Feed / Prompt / TestCase配对",
        "mode": "生成模式",
        "benchmark_version": "Benchmark版本",
        "score_schema_version": "Score Schema版本",
        "scenario_pack_version": "Scenario Pack版本",
        "preprocessor_version": "预处理版本",
        "generation_config_hash": "生成配置",
        "judge_versions": "Judge版本",
        "score_basis": "适用细则与有效维度权重",
        "benchmark_status": "Benchmark状态",
    }.get(str(kind), str(kind or UNKNOWN_VALUE))


def _comparability_detail(item: dict[str, Any]) -> str:
    if item.get("kind") == "unpaired_cases":
        return f"缺少配对 {item.get('count', 0)} 项"
    if item.get("kind") == "missing_runs":
        return f"基线{item.get('baseline', 0)} / 新版{item.get('candidate', 0)}"
    case = item.get("case")
    prefix = f"{case}：" if case else ""
    return f"{prefix}基线{_fmt(item.get('baseline'))} / 新版{_fmt(item.get('candidate'))}"


def _coverage_boundary(scene: dict[str, Any], report: dict[str, Any]) -> str:
    count = int(scene.get("comparable_pairs") or 0)
    if not count or report.get("status") == "not_comparable":
        return "不可比较"
    return f"基于{count}对；需结合样本代表性"


def _scene_conclusion(scene: dict[str, Any]) -> str:
    summary = scene.get("score_summary", {})
    delta = summary.get("delta_points")
    if delta is None:
        return "未取得/不可比较"
    return f"新版变化 {delta} 个百分点（{summary.get('classification', 'unclassified')}）"


def _direction(report: dict[str, Any]) -> str:
    scenario = report.get("score_summary", {}).get("scenario", {})
    delta = scenario.get("delta_points")
    if delta is None:
        return "未取得/不可比较"
    if delta > 0:
        return "提升"
    if delta < 0:
        return "下降"
    return "持平"


def _score_delta(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "baseline": None,
            "candidate": None,
            "delta_points": None,
            "delta_percent": None,
            "classification": "not_comparable",
        }
    return {
        "baseline": value.get("baseline"),
        "candidate": value.get("candidate"),
        "delta_points": value.get("delta_points"),
        "delta_percent": value.get("delta_percent"),
        "classification": value.get("classification", "unclassified"),
    }


def _scene_results_json(scene_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "scenario_id": item.get("scenario_id", ""),
            "scenario_name": item.get("scenario_name", item.get("scenario_id", "")),
            "mode": item.get("mode", "offline"),
            "score_summary": _score_delta(item.get("score_summary")),
            "dimensions": item.get("dimensions", []),
            "items": item.get("items", []),
            "evidence_ids": item.get("evidence_ids", []),
            "baseline_stats": item.get("baseline_stats", {"n": 0}),
            "candidate_stats": item.get("candidate_stats", {"n": 0}),
            "hard_gate_failures": item.get("hard_gate_failures", {"baseline": 0, "candidate": 0}),
            "comparable_pairs": item.get("comparable_pairs", 0),
        }
        for item in scene_results
    ]


def _recommendation(
    status: str,
    classified: dict[str, list[dict[str, Any]]],
    comparison: dict[str, Any],
) -> str:
    if status == "not_comparable":
        return "shadow"
    if status == "partial":
        return "retest"
    if classified.get("p2"):
        return "block"
    criteria = comparison.get("overall", {}).get("criteria", [])
    if any(
        item.get(side) in {"partially_scored", "unassessable", "uncovered"}
        for item in criteria
        for side in ("baseline_status", "candidate_status")
    ):
        return "retest"
    return "promote" if classified.get("p0") else "shadow"


def _distribution_placeholders(report: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label in ("baseline", "candidate"):
        stats = report.get("distribution_stats", {}).get(label, {})
        for key in (
            "n",
            "case_scores_percent",
            "mean",
            "median",
            "min",
            "max",
            "stdev",
            "p25",
            "p75",
            "success_rate",
        ):
            value = stats.get(key)
            result[f"{label}_{key}"] = UNKNOWN_VALUE if value is None else value
    return result


def _reporting_metric_placeholders(report: dict[str, Any]) -> dict[str, Any]:
    metrics = report.get("reporting_metric_results", {})
    baseline = metrics.get("baseline", {})
    candidate = metrics.get("candidate", {})
    left_p2, right_p2 = baseline.get("P.2", {}), candidate.get("P.2", {})
    left_p3 = baseline.get("P.3", {}).get("diagnostic_stability", {})
    right_p3 = candidate.get("P.3", {}).get("diagnostic_stability", {})
    left_p4, right_p4 = baseline.get("P.4", {}), candidate.get("P.4", {})
    left_rp1, right_rp1 = baseline.get("RP.1", {}), candidate.get("RP.1", {})
    left_rp2, right_rp2 = baseline.get("RP.2", {}), candidate.get("RP.2", {})
    left_rp3, right_rp3 = baseline.get("RP.3", {}), candidate.get("RP.3", {})
    return {
        "baseline_p2": _p2_summary(left_p2),
        "candidate_p2": _p2_summary(right_p2),
        "p2_delta": _p2_delta_text(left_p2, right_p2),
        "p2_explanation": "比较有效视频率、失败和重试，不进入单视频评分。",
        "baseline_p3": _p3_summary(left_p3),
        "candidate_p3": _p3_summary(right_p3),
        "p3_delta": _point_delta_text(
            left_p3.get("unstable_group_rate_percent"),
            right_p3.get("unstable_group_rate_percent"),
        ),
        "p3_explanation": "只比较评分口径一致的重复组；口径不一致组不进入分母。",
        "baseline_p4": _p4_summary(left_p4),
        "candidate_p4": _p4_summary(right_p4),
        "p4_delta": _metric_delta_text(
            (left_p4.get("generation_rtf") or {}).get("p50"),
            (right_p4.get("generation_rtf") or {}).get("p50"),
            " RTF",
        ),
        "p4_explanation": "只用独立的模型生成耗时计算RTF；旧合并耗时不代替。",
        "baseline_rp1": _rp1_summary(left_rp1),
        "candidate_rp1": _rp1_summary(right_rp1),
        "rp1_delta": _metric_delta_text(
            (left_rp1.get("first_frame_ms") or {}).get("p50"),
            (right_rp1.get("first_frame_ms") or {}).get("p50"),
            "ms",
        ),
        "rp1_explanation": "首帧越低越好；FPS需结合覆盖数判断。",
        "baseline_rp2": _rp2_summary(left_rp2),
        "candidate_rp2": _rp2_summary(right_rp2),
        "rp2_delta": _point_delta_text(
            left_rp2.get("automatic_recovery_rate_percent"),
            right_rp2.get("automatic_recovery_rate_percent"),
        ),
        "rp2_explanation": "未执行异常实验时不得推断恢复能力。",
        "baseline_rp3": _rp3_summary(left_rp3),
        "candidate_rp3": _rp3_summary(right_rp3),
        "rp3_delta": _metric_delta_text(
            (left_rp3.get("event_to_output_p95_ms") or {}).get("p50"),
            (right_rp3.get("event_to_output_p95_ms") or {}).get("p50"),
            "ms",
        ),
        "rp3_explanation": "仅比较网络Profile一致的持续端到端时延；越低越好。",
    }


def _p2_summary(value: dict[str, Any]) -> str:
    if not value:
        return UNKNOWN_VALUE
    return (
        f"完成{value.get('completed_run_count', 0)}/{value.get('planned_run_count', 0)}，"
        f"有效{value.get('valid_result_count', 0)}，"
        f"有效性未评{value.get('validity_unassessed_count', 0)}，"
        f"失败{value.get('generation_failure_count', 0)}，"
        f"无效{value.get('invalid_output_count', 0)}，重试{value.get('retry_count', 0)}"
    )


def _p3_summary(value: dict[str, Any]) -> str:
    if not value:
        return UNKNOWN_VALUE
    return (
        f"稳定{value.get('stable_group_count', 0)}，"
        f"不稳定{value.get('unstable_group_count', 0)}，"
        f"不稳定率{_fmt(value.get('unstable_group_rate_percent'))}%，"
        f"组内SD均值{_fmt((value.get('group_standard_deviation_points') or {}).get('mean'))}"
        f"个百分点，口径不一致{value.get('basis_mismatch_group_count', 0)}，"
        f"口径缺失{value.get('basis_unavailable_group_count', 0)}"
    )


def _p4_summary(value: dict[str, Any]) -> str:
    if not value or not int(value.get("run_count") or 0):
        return "本批不适用"
    observed = int((value.get("model_generation_elapsed_s") or {}).get("observed_count") or 0)
    if not observed:
        return f"离线Run {value.get('run_count', 0)}，缺少分离后的模型生成耗时"
    return (
        f"离线Run {value.get('run_count', 0)}，模型生成P50 "
        f"{_fmt((value.get('model_generation_elapsed_s') or {}).get('p50'))}s，RTF P50 "
        f"{_fmt((value.get('generation_rtf') or {}).get('p50'))}"
    )


def _rp1_summary(value: dict[str, Any]) -> str:
    if not value or not int(value.get("run_count") or 0):
        return "本批不适用"
    return (
        f"实时Run {value.get('run_count', 0)}，首帧P50 "
        f"{_fmt((value.get('first_frame_ms') or {}).get('p50'))}ms，FPS均值 "
        f"{_fmt((value.get('fps') or {}).get('mean'))}"
    )


def _rp2_summary(value: dict[str, Any]) -> str:
    if not value or not int(value.get("run_count") or 0):
        return "本批不适用"
    if not int(value.get("perturbation_run_count") or 0):
        return f"实时Run {value.get('run_count', 0)}，未执行异常实验"
    return (
        f"异常实验{value.get('perturbation_run_count', 0)}，恢复"
        f"{value.get('recovered_run_count', 0)}，恢复率"
        f"{_fmt(value.get('automatic_recovery_rate_percent'))}%"
    )


def _rp3_summary(value: dict[str, Any]) -> str:
    if not value or not int(value.get("run_count") or 0):
        return "本批不适用"
    observed = int((value.get("event_to_output_p95_ms") or {}).get("observed_count") or 0)
    if not observed:
        return f"实时Run {value.get('run_count', 0)}，缺少持续端到端时延证据"
    return (
        f"实时Run {value.get('run_count', 0)}，事件到输出P95的跨Run P50 "
        f"{_fmt((value.get('event_to_output_p95_ms') or {}).get('p50'))}ms，"
        f"网络Profile {len(value.get('network_profile_ids', []))}个"
    )


def _point_delta_text(left: Any, right: Any) -> str:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return "不可比较"
    return f"{float(right) - float(left):+.2f} 个百分点"


def _p2_delta_text(left: dict[str, Any], right: dict[str, Any]) -> str:
    if (
        int(left.get("validity_unassessed_count") or 0)
        or int(right.get("validity_unassessed_count") or 0)
    ):
        return "有效性证据不足"
    return _point_delta_text(
        left.get("valid_result_rate_percent"), right.get("valid_result_rate_percent")
    )


def _metric_delta_text(left: Any, right: Any, unit: str) -> str:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return "不可比较"
    return f"{float(right) - float(left):+.2f}{unit}"


def _score_with_status(status: Any, score: Any) -> str:
    labels = {
        "scored": "已评分",
        "partially_scored": "部分评分",
        "not_applicable": "不适用",
        "inapplicable_to_batch": "本批不适用",
        "unassessable": "不可评",
        "uncovered": "未覆盖",
    }
    return f"{labels.get(str(status), str(status))}；{_pct(score)}"


def _item_label(item: dict[str, Any]) -> str:
    item_id = item.get("item_id") or item.get("case_number", "")
    item_name = item.get("item_name") or ""
    return f"{item_id} {item_name}".strip()


def _fmt(value: Any) -> str:
    if value is None:
        return UNKNOWN_VALUE
    if isinstance(value, list):
        return "、".join(str(item) for item in value) if value else UNKNOWN_VALUE
    return str(value)


def _pct(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return UNKNOWN_VALUE
    return f"{float(value):.2f}%"


def _points(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return UNKNOWN_VALUE
    return f"{float(value):+.2f}"


def _join(values: list[str]) -> str:
    return ", ".join(values)


def _bucket_ids(items: list[dict[str, Any]]) -> str:
    values = [item.get("item_id") for item in items if item.get("item_id")]
    return "、".join(values[:8]) if values else "无达到显著阈值的项目"


def _bucket_summary(label: str, items: list[dict[str, Any]]) -> str:
    if not items:
        return f"无{label}项目。"
    return f"{label}项目：{_bucket_ids(items)}。"


def _now() -> str:
    from ..time import utc_now

    return utc_now()
