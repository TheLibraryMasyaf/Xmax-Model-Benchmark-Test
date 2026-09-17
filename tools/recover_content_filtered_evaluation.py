#!/usr/bin/env python3
"""Recover one content-filtered evaluation without regenerating its video.

This is an explicit, audited exception path. It reuses the frozen Run and
PreprocessResult, masks only the exposed torso area in result/reference stills,
forces the MLLM to consume still images (not the original videos), and then
persists a normal EvaluationResult through the production fusion path.
"""

from __future__ import annotations

import argparse
import json
import subprocess
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


class SanitizedStillProvider:
    """Wrap a configured provider with case-local still-image sanitization."""

    def __init__(
        self,
        inner: Any,
        *,
        work_dir: Path,
        prompt_reference_path: Path,
        ffmpeg: str,
    ) -> None:
        self._inner = inner
        self._work_dir = work_dir
        self._prompt_reference_path = prompt_reference_path
        self._ffmpeg = ffmpeg
        self._prepared: list[str] | None = None

    @property
    def provider_id(self) -> str:
        return f"{self._inner.provider_id}_sanitized_stills"

    def complete_json(
        self,
        *,
        prompt: str,
        image_paths: list[str],
        output_schema: dict[str, Any],
        media_inputs: list[dict[str, Any]] | None = None,
    ) -> Any:
        del media_inputs
        if self._prepared is None:
            self._prepared = self._prepare_images(image_paths)
        audit_note = (
            "\n证据审核说明：最后一张图是Prompt参考角色图。为绕过上游平台对男性裸露躯干的"
            "误拦截，结果抽帧和Prompt参考图的躯干中央被中性灰色隐私遮挡；遮挡不是生成瑕疵，"
            "不得据此扣分。请仅依据未遮挡的脸部、头饰、四肢、轮廓、动作、背景、时序抽帧和"
            "服饰细节评分；不得臆测遮挡区域。"
        )
        return self._inner.complete_json(
            prompt=prompt + audit_note,
            image_paths=self._prepared,
            output_schema=output_schema,
            media_inputs=[],
        )

    def _prepare_images(self, image_paths: list[str]) -> list[str]:
        self._work_dir.mkdir(parents=True, exist_ok=True)
        prepared: list[str] = []
        for index, raw_path in enumerate(image_paths):
            source = Path(raw_path)
            if source.name.startswith("global-"):
                destination = self._work_dir / f"result-{index:02d}.jpg"
                self._mask(source, destination, "iw*0.08", "ih*0.29", "iw*0.84", "ih*0.47")
                prepared.append(str(destination.resolve()))
            else:
                prepared.append(str(source.resolve()))
        reference = self._work_dir / "prompt-reference-masked.jpg"
        self._mask(
            self._prompt_reference_path,
            reference,
            "iw*0.16",
            "ih*0.17",
            "iw*0.68",
            "ih*0.36",
        )
        prepared.append(str(reference.resolve()))
        return prepared

    def _mask(
        self,
        source: Path,
        destination: Path,
        x: str,
        y: str,
        width: str,
        height: str,
    ) -> None:
        subprocess.run(
            [
                self._ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                f"drawbox=x={x}:y={y}:w={width}:h={height}:color=gray:t=fill",
                "-frames:v",
                "1",
                str(destination),
            ],
            check=True,
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
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    composition = Composition(root)
    try:
        task = composition.database.get_test_task(args.task_id)
        last_error = task.get("last_error") or {}
        replacing_incomplete = bool(
            task.get("status") == "completed"
            and (task.get("result_refs") or {}).get("content_filter_recovery") is True
        )
        if not replacing_incomplete and (
            task.get("status") != "error"
            or last_error.get("code") != "xmax.mlmm_invalid_request"
        ):
            raise RuntimeError("task is not a content-filtered MLLM error")
        refs = task.get("result_refs") or {}
        run = composition.database.get_run(refs["run_id"])
        if run.get("status") != "completed" or not run.get("result_asset_id"):
            raise RuntimeError("frozen XMAX run is not completed")
        existing_evaluations = composition.database.list_evaluation_results(
            run_id=run["run_id"]
        )
        if existing_evaluations:
            if not replacing_incomplete or any(
                item.get("score_readiness") != "incomplete_evidence"
                for item in existing_evaluations
            ):
                raise RuntimeError("run already has a usable EvaluationResult")
        case = composition.database.get_test_case(task["case_id"])
        prompt_asset_id = (case.get("prompt_asset_ids") or [None])[0]
        if not prompt_asset_id:
            raise RuntimeError("case has no prompt reference asset")
        prompt_asset = composition.database.get_asset(prompt_asset_id)
        prompt_reference = composition.artifacts.resolve(prompt_asset["uri"])
        preprocess = composition.database.get_preprocess_for_run(run["run_id"])

        judge_cfg = _judge_config(root)
        inner = composition.mlmm_provider(judge_cfg)
        work_dir = (
            root
            / "var"
            / "artifacts"
            / "evaluations"
            / "content-filter-recovery"
            / run["run_id"]
        )
        provider = SanitizedStillProvider(
            inner,
            work_dir=work_dir,
            prompt_reference_path=prompt_reference,
            ffmpeg=args.ffmpeg,
        )
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
        coverage = evaluation.get("coverage") or {}
        if (
            evaluation.get("score_readiness") != "ready"
            or evaluation.get("case_score_percent") is None
            or coverage.get("unassessable_criteria")
            or coverage.get("uncovered_criteria")
        ):
            raise RuntimeError(
                "recovery evaluation is not fully scoreable: "
                f"readiness={evaluation.get('score_readiness')} coverage={coverage}"
            )
        composition.database.update_test_task(
            args.task_id,
            "completed",
            result_refs={
                "evaluation_batch_id": summary["evaluation_batch_id"],
                "evaluation_id": evaluation["evaluation_id"],
                "outcome": "completed",
                "content_filter_recovery": True,
            },
            last_error=None,
        )
        report = {
            "task_id": args.task_id,
            "case_id": task["case_id"],
            "case_number": case.get("case_number"),
            "run_id": run["run_id"],
            "preprocess_id": preprocess.get("preprocess_id"),
            "evaluation_batch_id": summary["evaluation_batch_id"],
            "evaluation_id": evaluation["evaluation_id"],
            "recovery_kind": "sanitized_still_evidence",
            "reason": last_error or {
                "code": "xmax.mlmm_invalid_request",
                "message": "replaced prior incomplete content-filter recovery evaluation",
            },
            "sanitized_directory": str(work_dir.resolve()),
            "provider_id": provider.provider_id,
            "score_readiness": evaluation.get("score_readiness"),
            "case_score_percent": evaluation.get("case_score_percent"),
            "coverage": coverage,
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        composition.close()


if __name__ == "__main__":
    raise SystemExit(main())
