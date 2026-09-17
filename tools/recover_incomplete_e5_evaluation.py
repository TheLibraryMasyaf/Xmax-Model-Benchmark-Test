#!/usr/bin/env python3
"""Re-evaluate a completed task whose selected result mislabels E5 as unassessable."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from xmax_test.cli import Composition
from xmax_test.evaluation.fusion import JudgmentFusion
from xmax_test.evaluation.orchestrator import EvaluationOrchestrator
from xmax_test.judges.mlmm import MlmmJudge
from xmax_test.judges.plugins.audio_integrity import AudioIntegrityJudge
from xmax_test.judges.plugins.run_metrics import RunMetricsJudge
from xmax_test.judges.plugins.video_quality import VideoQualityJudge
from xmax_test.judges.registry import JudgeRegistry
from xmax_test.judges.worker import JudgeWorker


class ApplicabilityAwareProvider:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def provider_id(self) -> str:
        return f"{self._inner.provider_id}_e5_applicability_recovery"

    def complete_json(
        self,
        *,
        prompt: str,
        image_paths: list[str],
        output_schema: dict[str, Any],
        media_inputs: list[dict[str, Any]] | None = None,
    ) -> Any:
        clarification = (
            "\nE5适用性补充合同：criterion_results每项额外输出applicable布尔值。"
            "只有明确满足该criterion合同中的not_applicable_when时，才输出applicable=false、"
            "assessable=false、score=null，并在证据中说明不适用事实。只要画面存在可见手势、"
            "肢体或镜头运动，E5.2就属于可评，不能因为没有推拉或碰撞而标为不可评；应按运动方向、"
            "连续性、速度变化、惯性和状态演化给0/1/2分。E5.3不能仅因没有火焰、烟雾或液体而"
            "标为不可评；若可见头发、服装、皮肤、光照、阴影、遮挡或反射，应据其响应给0/1/2分。"
            "E5.1在主体没有地面、物体或其他主体接触/支撑/碰撞关系时可标为不适用。"
        )
        return self._inner.complete_json(
            prompt=prompt + clarification,
            image_paths=image_paths,
            output_schema=output_schema,
            media_inputs=media_inputs,
        )


def _judge_config(root: Path) -> dict[str, Any]:
    payload = json.loads((root / "config" / "judges.json").read_text(encoding="utf-8"))
    return next(
        item
        for item in payload["judges"]
        if item.get("enabled") and item.get("kind") in {"mlmm", "mlmm_cli"}
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    composition = Composition(root)
    try:
        task = composition.database.get_test_task(args.task_id)
        if task.get("status") != "completed":
            raise RuntimeError("task must already be completed")
        refs = task.get("result_refs") or {}
        current = composition.database.get_evaluation_result(refs["evaluation_id"])
        coverage = current.get("coverage") or {}
        unassessable = list(coverage.get("unassessable_criteria") or [])
        if not unassessable or any(not item.startswith("E5.") for item in unassessable):
            raise RuntimeError("selected evaluation is not an E5-only incomplete result")
        run = composition.database.get_run(refs["run_id"])
        preprocess = composition.database.get_preprocess_for_run(run["run_id"])

        judge_cfg = _judge_config(root)
        provider = ApplicabilityAwareProvider(composition.mlmm_provider(judge_cfg))
        registry = JudgeRegistry()
        registry.register(
            MlmmJudge(
                provider,
                artifact_store=composition.artifacts,
                log_dir=root / "var" / "logs" / "mlmm",
                judge_id=judge_cfg["judge_id"],
                version=judge_cfg["version"],
                supported_dimensions=judge_cfg.get("supported_dimensions", []),
                supported_criteria=judge_cfg.get("supported_criteria", []),
                supported_modes=judge_cfg.get("supported_modes", ["offline", "realtime"]),
                max_retries=int(judge_cfg.get("max_retries", 2)),
                calibration_path=root / judge_cfg["calibration"]["path"],
                max_examples_per_dimension=int(
                    judge_cfg.get("calibration", {}).get("max_examples_per_dimension", 2)
                ),
            )
        )
        registry.register(VideoQualityJudge())
        registry.register(RunMetricsJudge())
        registry.register(AudioIntegrityJudge())
        worker = JudgeWorker(registry, composition.artifacts)
        orchestrator = EvaluationOrchestrator(
            composition.database,
            composition.artifacts,
            composition.benchmark,
            composition.scenario_pack,
            registry,
            worker,
            composition.preprocess_service(),
            fusion=JudgmentFusion(),
            recipe_resolver=composition.recipes,
            budget_gate=composition.evaluation_budget_gate(),
            clock=composition.clock,
        )
        summary = orchestrator.evaluate_runs(
            [run],
            preprocess_by_run={run["run_id"]: preprocess},
        )
        if summary.get("failed") or len(summary.get("results") or []) != 1:
            raise RuntimeError(f"recovery evaluation failed: {summary.get('errors')}")
        evaluation = summary["results"][0]
        new_coverage = evaluation.get("coverage") or {}
        if (
            evaluation.get("score_readiness") != "ready"
            or evaluation.get("case_score_percent") is None
            or new_coverage.get("unassessable_criteria")
            or new_coverage.get("uncovered_criteria")
        ):
            raise RuntimeError(
                "recovery evaluation is not fully scoreable: "
                f"readiness={evaluation.get('score_readiness')} coverage={new_coverage}"
            )
        composition.database.update_test_task(
            args.task_id,
            "completed",
            result_refs={
                "evaluation_batch_id": summary["evaluation_batch_id"],
                "evaluation_id": evaluation["evaluation_id"],
                "outcome": "completed",
                "e5_applicability_recovery": True,
            },
            last_error=None,
        )
        report = {
            "task_id": args.task_id,
            "case_number": (task.get("payload") or {}).get("case", {}).get("case_number"),
            "run_id": run["run_id"],
            "replaced_evaluation_id": current["evaluation_id"],
            "evaluation_batch_id": summary["evaluation_batch_id"],
            "evaluation_id": evaluation["evaluation_id"],
            "recovery_kind": "e5_applicability_clarification",
            "previous_unassessable_criteria": unassessable,
            "score_readiness": evaluation.get("score_readiness"),
            "case_score_percent": evaluation.get("case_score_percent"),
            "coverage": new_coverage,
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if report_path.is_file():
            existing = json.loads(report_path.read_text(encoding="utf-8"))
        existing.append(report)
        report_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        composition.close()


if __name__ == "__main__":
    raise SystemExit(main())
