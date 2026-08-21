"""Deterministic Judge for generation and realtime runtime facts.

This plugin never infers latency, cost or stability from contact sheets.  It
only scores facts recorded by the generation adapters.  When the required
experiment was not run (for example a three-second sample for long-session
stability), it returns an explicit unassessable judgment.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any


class RunMetricsJudge:
    VERSION = "0.2.0-criterion-shadow"

    def manifest(self) -> dict[str, Any]:
        return {
            "judge_id": "run-metrics",
            "version": self.VERSION,
            "kind": "metric",
            "supported_dimensions": [
                "C1",
                "O3",
                "O5",
                "R1",
                "R2",
                "R3",
                "R5",
                "R6",
                "R7",
            ],
            "supported_criteria": [
                "C1.1",
                "O3.3",
                "O5.1",
                "R1.1",
                "R1.2",
                "R2.1",
                "R3.1",
                "R5.1",
                "R6.3",
                "R7.1",
            ],
            "supported_modes": ["offline", "realtime"],
            "required_inputs": ["generation_run"],
            "entrypoint": "xmax_test.judges.plugins.run_metrics:RunMetricsJudge",
            "timeout_seconds": 30,
        }

    def evaluate(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        dimension = context.get("dimension_id")
        method = getattr(self, f"_score_{str(dimension).lower()}", None)
        if method is None:
            criteria = [
                item["criterion_id"]
                for item in context.get("dimension_contract", {}).get("criteria", [])
                if item.get("criterion_id")
            ]
            return [
                self._unassessable(
                    dimension,
                    criteria or [f"{dimension}.unknown"],
                    "unsupported runtime dimension",
                )
            ]
        return [method(context)]

    def _score_c1(self, context: dict[str, Any]) -> dict[str, Any]:
        run = context.get("run", {})
        valid = run.get("status") == "completed" and bool(run.get("result_asset_id"))
        valid_score = 2.0 if valid else 0.0
        metrics = {"valid_result": valid}
        description = "Run终态和结果媒体登记来自生成日志；跨输入稳定性由批次级Judge评估。"
        return self._multi_judgment(
            "C1",
            [
                self._criterion(
                    "C1.1",
                    valid_score,
                    "valid_result" if valid else "no_valid_result",
                    metrics,
                    description,
                ),
            ],
        )

    def _score_o3(self, context: dict[str, Any]) -> dict[str, Any]:
        paths = context.get("asset_paths", {})
        result_duration = _duration(paths.get("result_video"))
        source_duration = _duration(paths.get("edited_video"))
        if not result_duration or not source_duration:
            return self._unassessable("O3", "O3.3", "result/source duration unavailable")
        ratio = result_duration / source_duration
        if 0.98 <= ratio <= 1.02:
            score, verdict = 2.0, "timeline_duration_preserved"
        elif 0.90 <= ratio <= 1.10:
            score, verdict = 1.0, "minor_timeline_duration_difference"
        else:
            score, verdict = 0.0, "timeline_truncated_or_extended"
        return self._judgment(
            "O3",
            "O3.3",
            score,
            verdict,
            {
                "result_duration_s": round(result_duration, 3),
                "source_duration_s": round(source_duration, 3),
                "duration_ratio": round(ratio, 4),
            },
            "比较被编辑视频与结果视频的可解码时长；持续效果和后段漂移由视觉Judge补充。",
        )

    def _score_o5(self, context: dict[str, Any]) -> dict[str, Any]:
        run = context.get("run", {})
        metrics = run.get("metrics", {})
        origin = run.get("origin", "")
        elapsed = metrics.get("generation_elapsed_s")
        source_duration = _duration(context.get("asset_paths", {}).get("edited_video"))
        if not isinstance(elapsed, (int, float)) or not source_duration:
            if origin in {"feishu_import", "local_import", "stage_manifest_import"}:
                return self._not_applicable(
                    "O5", "O5.1", "imported result has no generation timing experiment"
                )
            return self._unassessable(
                "O5", "O5.1", "generation elapsed time or source duration unavailable"
            )
        good_limit = max(60.0, source_duration * 20.0)
        acceptable_limit = max(180.0, source_duration * 60.0)
        if elapsed <= good_limit:
            score, verdict = 2.0, "generation_time_within_good_limit"
        elif elapsed <= acceptable_limit:
            score, verdict = 1.0, "generation_time_noticeable_but_acceptable"
        else:
            score, verdict = 0.0, "generation_time_exceeds_acceptable_limit"
        return self._judgment(
            "O5",
            "O5.1",
            score,
            verdict,
            {
                "generation_elapsed_s": round(float(elapsed), 3),
                "source_duration_s": round(source_duration, 3),
                "good_limit_s": round(good_limit, 3),
                "acceptable_limit_s": round(acceptable_limit, 3),
            },
            "提交到结果可用的真实墙钟时间按被编辑视频时长归一化。",
        )

    def _score_r1(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        connect = metrics.get("connect_ms")
        first_frame = metrics.get("first_frame_ms")
        criteria = []
        if isinstance(connect, (int, float)):
            score = 2.0 if connect <= 1000 else (1.0 if connect <= 3000 else 0.0)
            criteria.append(
                self._criterion(
                    "R1.1",
                    score,
                    "session_connect_time_recorded",
                    {"connect_ms": connect},
                    "使用会话建立完成的单调时钟时间戳。",
                )
            )
        else:
            criteria.extend(
                self._unassessable("R1", "R1.1", "connect timestamp unavailable")[
                    "criterion_results"
                ]
            )
        if isinstance(first_frame, (int, float)) and isinstance(connect, (int, float)):
            effective = max(0.0, float(first_frame) - float(connect))
            score = 2.0 if effective <= 500 else (1.0 if effective <= 1500 else 0.0)
            criteria.append(
                self._criterion(
                    "R1.2",
                    score,
                    "first_effective_result_time_recorded",
                    {"post_connect_first_frame_ms": effective},
                    "使用连接完成到首个输出帧的单调时钟差。",
                )
            )
        else:
            criteria.extend(
                self._unassessable("R1", "R1.2", "connect/first-frame timestamps unavailable")[
                    "criterion_results"
                ]
            )
        return self._multi_judgment("R1", criteria)

    def _score_r2(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        latency = metrics.get("first_output_change_ms")
        if not isinstance(latency, (int, float)):
            return self._unassessable(
                "R2", "R2.1", "no instrumented input-to-output change latency"
            )
        if latency <= 200:
            score, verdict = 2.0, "responsive_interaction"
        elif latency <= 600:
            score, verdict = 1.0, "noticeable_interaction_latency"
        else:
            score, verdict = 0.0, "slow_interaction_response"
        return self._judgment(
            "R2",
            "R2.1",
            score,
            verdict,
            {"first_output_change_ms": latency},
            "使用输入事件到首个可见输出变化的时钟差。",
        )

    def _score_r3(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        ratio = metrics.get("track_send_success_ratio")
        if not isinstance(ratio, (int, float)):
            return self._unassessable("R3", "R3.1", "no tracked interaction send facts")
        if ratio >= 0.98:
            score, verdict = 2.0, "interaction_events_delivered"
        elif ratio >= 0.85:
            score, verdict = 1.0, "some_interaction_events_lost"
        else:
            score, verdict = 0.0, "interaction_delivery_unreliable"
        return self._judgment(
            "R3",
            "R3.1",
            score,
            verdict,
            {"track_send_success_ratio": round(float(ratio), 4)},
            "这里只验证控制事件送达；作用对象、方向与可见效果由视觉Judge复核。",
        )

    def _score_r5(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        recovery_ms = metrics.get("recovery_ms")
        perturbations = int(metrics.get("perturbation_count") or 0)
        if perturbations == 0 or not isinstance(recovery_ms, (int, float)):
            return self._unassessable(
                "R5", "R5.1", "no versioned recovery perturbation was executed"
            )
        if recovery_ms <= 500:
            score, verdict = 2.0, "fast_recovery"
        elif recovery_ms <= 2000:
            score, verdict = 1.0, "recovered_with_visible_delay"
        else:
            score, verdict = 0.0, "slow_or_failed_recovery"
        return self._judgment(
            "R5",
            "R5.1",
            score,
            verdict,
            {"recovery_ms": recovery_ms, "perturbation_count": perturbations},
            "使用版本化异常脚本的恢复时间；没有注入异常时不臆测满分。",
        )

    def _score_r6(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        duration = metrics.get("session_duration_s")
        if not isinstance(duration, (int, float)) or duration < 60:
            return self._unassessable("R6", "R6.3", "session shorter than the 60s shadow minimum")
        fps_cv = metrics.get("fps_window_cv")
        if not isinstance(fps_cv, (int, float)):
            return self._unassessable("R6", "R6.3", "long-session window metrics unavailable")
        if fps_cv <= 0.05:
            score, verdict = 2.0, "stable_long_session_runtime"
        elif fps_cv <= 0.15:
            score, verdict = 1.0, "minor_long_session_degradation"
        else:
            score, verdict = 0.0, "long_session_runtime_degraded"
        return self._judgment(
            "R6",
            "R6.3",
            score,
            verdict,
            {"session_duration_s": duration, "fps_window_cv": fps_cv},
            "比较长会话各时间窗的帧率稳定性；身份和画质漂移由视觉Judge补充。",
        )

    def _score_r7(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        fps = metrics.get("fps")
        frames = metrics.get("frames_captured")
        dropped = metrics.get("dropped_frames")
        if not isinstance(fps, (int, float)):
            return self._unassessable("R7", "R7.1", "captured output FPS unavailable")
        drop_ratio = (
            float(dropped) / max(float(frames) + float(dropped), 1.0)
            if isinstance(frames, (int, float)) and isinstance(dropped, (int, float))
            else None
        )
        if fps >= 24 and (drop_ratio is None or drop_ratio <= 0.02):
            score, verdict = 2.0, "stable_realtime_framerate"
        elif fps >= 15 and (drop_ratio is None or drop_ratio <= 0.10):
            score, verdict = 1.0, "usable_with_frame_loss"
        else:
            score, verdict = 0.0, "low_or_unstable_framerate"
        return self._judgment(
            "R7",
            "R7.1",
            score,
            verdict,
            {
                "fps": fps,
                "frames_captured": frames,
                "dropped_frames": dropped,
                "drop_ratio": drop_ratio,
            },
            "使用浏览器输出帧时间戳；拖影和重影由视觉Judge补充。",
        )

    def _judgment(
        self,
        dimension: str,
        criterion_id: str,
        score: float,
        verdict: str,
        metrics: dict[str, Any],
        description: str,
    ) -> dict[str, Any]:
        criterion = self._criterion(criterion_id, score, verdict, metrics, description)
        return self._multi_judgment(dimension, [criterion])

    @staticmethod
    def _criterion(
        criterion_id: str,
        score: float,
        verdict: str,
        metrics: dict[str, Any],
        description: str,
    ) -> dict[str, Any]:
        return {
            "criterion_id": criterion_id,
            "verdict": verdict,
            "score": score,
            "confidence": 0.95,
            "assessable": True,
            "evidence": [{"description": description}],
            "raw_metrics": metrics,
        }

    @staticmethod
    def _multi_judgment(dimension: str, criteria: list[dict[str, Any]]) -> dict[str, Any]:
        scores = [item["score"] for item in criteria if item.get("assessable")]
        evidence = [item for criterion in criteria for item in criterion.get("evidence", [])]
        return {
            "dimension_id": dimension,
            "verdict": "criterion_scores_recorded" if scores else "unassessable",
            "score": sum(scores) / len(scores) if scores else None,
            "confidence": 0.95,
            "assessable": bool(scores),
            "evidence": evidence,
            "raw_metrics": {},
            "criterion_results": criteria,
        }

    @staticmethod
    def _unassessable(
        dimension: str, criterion_ids: str | list[str], reason: str
    ) -> dict[str, Any]:
        if isinstance(criterion_ids, str):
            criterion_ids = [criterion_ids]
        criteria = [
            {
                "criterion_id": criterion_id,
                "verdict": "unassessable",
                "score": None,
                "confidence": None,
                "assessable": False,
                "evidence": [{"description": reason}],
                "raw_metrics": {},
            }
            for criterion_id in criterion_ids
        ]
        return RunMetricsJudge._multi_judgment(dimension, criteria)

    @staticmethod
    def _not_applicable(dimension: str, criterion_id: str, reason: str) -> dict[str, Any]:
        result = RunMetricsJudge._unassessable(dimension, criterion_id, reason)
        result["criterion_results"][0]["applicable"] = False
        result["criterion_results"][0]["verdict"] = "not_applicable"
        return result


def _duration(path: str | None) -> float | None:
    if not path:
        return None
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            return None
        return float(json.loads(completed.stdout)["format"]["duration"])
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return None
