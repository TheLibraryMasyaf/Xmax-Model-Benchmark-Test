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

REPORTER_VERSION = "0.2.0-criterion-scores"
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
        "criterion_results": comparison.get("overall", {}).get("criteria", []),
        "p0_improvements": classified.get("p0", []),
        "p1_ties": classified.get("p1", []),
        "p2_regressions": classified.get("p2", []),
        "scene_results": _scene_results_json(comparison.get("scene_results", [])),
        "human_signal_summary": {"count": 0, "overrides": 0},
        "release_recommendation": _recommendation(status, classified),
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
    }

    text = template_text
    text = _render_scene_sections(text, report)
    text = _render_score_rows(text, report)
    text = _render_scene_coverage(text, report)
    text = _render_criterion_rows(text, report)
    text = _render_item_rows(text, "p0", report.get("p0_improvements", []))
    text = _render_item_rows(text, "p1", report.get("p1_ties", []))
    text = _render_item_rows(text, "p2", report.get("p2_regressions", []))
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
            "{{ scenario_name }}": scene.get("scenario_id", UNKNOWN_VALUE),
            "{{ scenario_goal }}": "未取得/不可比较",
            "{{ profile_and_rules }}": "未取得/不可比较",
            "{{ dimension }}": UNKNOWN_VALUE,
            "{{ scene_improvements }}": UNKNOWN_VALUE,
            "{{ scene_ties }}": UNKNOWN_VALUE,
            "{{ scene_regressions }}": UNKNOWN_VALUE,
            "{{ run_ids / evaluation_ids / artifact_uris }}": (
                ", ".join(scene.get("evidence_ids", [])) or "无"
            ),
            "{{ scene_conclusion }}": _scene_conclusion(scene),
            "{{ class }}": UNKNOWN_VALUE,
        }
        for placeholder, value in replacements.items():
            item = item.replace(placeholder, value)
        item = re.sub(r"\{\{\s*\.\.\.\s*\}\}", UNKNOWN_VALUE, item)
        sections.append(item)
    return text[:start_index] + "\n".join(sections) + text[end_index:]


def _render_score_rows(text: str, report: dict[str, Any]) -> str:
    summary = report.get("score_summary", {})
    canonical = summary.get("canonical", {})
    scenario = summary.get("scenario", {})
    canonical_row = _score_row("Canonical Score", canonical)
    scenario_row = _score_row("请求场景加权Scenario Score", scenario)
    pattern = re.compile(
        r"\| Canonical Score \|.*?\|\n\| 请求场景加权Scenario Score \|.*?\|\n",
        re.DOTALL,
    )
    return pattern.sub(canonical_row + "\n" + scenario_row + "\n", text)


def _render_scene_coverage(text: str, report: dict[str, Any]) -> str:
    rows = []
    for scene in report.get("scene_results", []):
        rows.append(
            f"| `{scene.get('scenario_id', '')}` | `{scene.get('mode', 'offline')}` | "
            f"`{scene.get('comparable_pairs', 0)}` | `{scene.get('comparable_pairs', 0)}` | "
            f"`{scene.get('comparable_pairs', 0)}` | `{UNKNOWN_VALUE}` | `{UNKNOWN_VALUE}` |"
        )
    pattern = re.compile(r"\| `\{\{ scene \}\}`.*?\|\n", re.DOTALL)
    return pattern.sub("\n".join(rows) + "\n" if rows else "", text)


def _render_criterion_rows(text: str, report: dict[str, Any]) -> str:
    rows = []
    for item in report.get("criterion_results", []):
        rows.append(
            f"| `{item.get('dimension_id', '')}` | `{item.get('criterion_id', '')}` | "
            f"{item.get('criterion_name') or UNKNOWN_VALUE} | {_fmt(item.get('baseline'))} | "
            f"{_fmt(item.get('candidate'))} | {_fmt(item.get('delta_points'))} | "
            f"{item.get('baseline_assessable_count', 0)} | "
            f"{item.get('candidate_assessable_count', 0)} | "
            f"{item.get('classification', 'unclassified')} | "
            f"{_fmt(item.get('evidence_ids', []))} |"
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
            f"| {index} | `{item.get('item_id') or item.get('case_number', '')}` | {_fmt(item.get('baseline'))} | "
            f"{_fmt(item.get('candidate'))} | {_fmt(item.get('delta_points'))} | "
            f"{UNKNOWN_VALUE} | {_fmt(item.get('evidence_ids', []))} | {UNKNOWN_VALUE} |"
            for index, item in enumerate(items, start=1)
        ]
    elif bucket == "p1":
        pattern = re.compile(r"\| `\{\{ item \}\}`.*?持平 \|\n", re.DOTALL)
        rows = [
            f"| `{item.get('item_id') or item.get('case_number', '')}` | {_fmt(item.get('baseline'))} | "
            f"{_fmt(item.get('candidate'))} | {_fmt(item.get('delta_points'))} | "
            f"{UNKNOWN_VALUE} | {_fmt(item.get('evidence_ids', []))} | 持平 |"
            for item in items
        ]
    else:
        pattern = re.compile(r"\| `blocker/high/medium/low` \| `\{\{ item \}\}`.*?\|\n", re.DOTALL)
        rows = [
            f"| {item.get('severity', 'medium')} | `{item.get('item_id') or item.get('case_number', '')}` | "
            f"{_fmt(item.get('baseline'))} | {_fmt(item.get('candidate'))} | "
            f"{_fmt(item.get('delta_points'))} | `{UNKNOWN_VALUE}` | "
            f"`{'invalid-result-block-score' if item.get('new_hard_gate_failure') else 'none'}` | "
            f"{_fmt(item.get('evidence_ids', []))} | `{UNKNOWN_VALUE}` |"
            for item in items
        ]
    return pattern.sub("\n".join(rows) + "\n" if rows else "无满足条件的项目\n", text)


def _score_row(label: str, delta: dict[str, Any]) -> str:
    return (
        f"| {label} | `{_fmt(delta.get('baseline'))}` | `{_fmt(delta.get('candidate'))}` | "
        f"`{_fmt(delta.get('delta_points'))}` | `{_fmt(delta.get('delta_percent'))}` | "
        f"`{delta.get('classification', 'unclassified')}` |"
    )


def _comparability_conclusion(report: dict[str, Any]) -> str:
    comparability = report.get("comparability", {})
    if comparability.get("comparable"):
        return "可比，可以计算正式升降结论。"
    differences = comparability.get("differences", [])
    detail = "; ".join(str(d) for d in differences[:5])
    return f"不可比：{detail}" if detail else "不可比，仅报告事实。"


def _scene_conclusion(scene: dict[str, Any]) -> str:
    summary = scene.get("score_summary", {})
    delta = summary.get("delta_points")
    if delta is None:
        return "未取得/不可比较"
    return f"新版变化 {delta} 分（{summary.get('classification', 'unclassified')}）"


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
            "mode": item.get("mode", "offline"),
            "score_summary": _score_delta(item.get("score_summary")),
            "items": item.get("items", []),
            "evidence_ids": item.get("evidence_ids", []),
            "baseline_stats": item.get("baseline_stats", {"n": 0}),
            "candidate_stats": item.get("candidate_stats", {"n": 0}),
            "hard_gate_failures": item.get("hard_gate_failures", {"baseline": 0, "candidate": 0}),
            "comparable_pairs": item.get("comparable_pairs", 0),
        }
        for item in scene_results
    ]


def _recommendation(status: str, classified: dict[str, list[dict[str, Any]]]) -> str:
    if status == "not_comparable":
        return "shadow"
    if classified.get("p2"):
        return "block"
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


def _fmt(value: Any) -> str:
    if value is None:
        return UNKNOWN_VALUE
    return str(value)


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
