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
    VERSION = "0.1.0-shadow"

    def manifest(self) -> dict[str, Any]:
        return {
            "judge_id": "run-metrics",
            "version": self.VERSION,
            "kind": "metric",
            "supported_dimensions": [
                "C1",
                "O3",
                "O4",
                "O5",
                "R1",
                "R2",
                "R3",
                "R5",
                "R6",
                "R7",
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
            return [self._unassessable(dimension, "unsupported runtime dimension")]
        return [method(context)]

    def _score_c1(self, context: dict[str, Any]) -> dict[str, Any]:
        run = context.get("run", {})
        valid = run.get("status") == "completed" and bool(run.get("result_asset_id"))
        retry_count = int(run.get("metrics", {}).get("retry_count") or 0)
        if not valid:
            score, verdict = 0.0, "no_valid_result"
        elif retry_count:
            score, verdict = 1.0, "valid_after_retry"
        else:
            score, verdict = 2.0, "valid_result_first_attempt"
        return self._judgment(
            "C1",
            score,
            verdict,
            {"valid_result": valid, "retry_count": retry_count},
            "Run终态、结果媒体登记和重试次数来自生成日志；是否原画直出仍由视觉Judge复核。",
        )

    def _score_o3(self, context: dict[str, Any]) -> dict[str, Any]:
        paths = context.get("asset_paths", {})
        result_duration = _duration(paths.get("result_video"))
        source_duration = _duration(paths.get("edited_video"))
        if not result_duration or not source_duration:
            return self._unassessable("O3", "result/source duration unavailable")
        ratio = result_duration / source_duration
        if 0.98 <= ratio <= 1.02:
            score, verdict = 2.0, "timeline_duration_preserved"
        elif 0.90 <= ratio <= 1.10:
            score, verdict = 1.0, "minor_timeline_duration_difference"
        else:
            score, verdict = 0.0, "timeline_truncated_or_extended"
        return self._judgment(
            "O3",
            score,
            verdict,
            {
                "result_duration_s": round(result_duration, 3),
                "source_duration_s": round(source_duration, 3),
                "duration_ratio": round(ratio, 4),
            },
            "比较被编辑视频与结果视频的可解码时长；持续效果和后段漂移由视觉Judge补充。",
        )

    def _score_o4(self, context: dict[str, Any]) -> dict[str, Any]:
        return self._unassessable(
            "O4",
            "repeat stability requires a completed repeat group and is aggregated after per-run evaluation",
        )

    def _score_o5(self, context: dict[str, Any]) -> dict[str, Any]:
        run = context.get("run", {})
        metrics = run.get("metrics", {})
        case = context.get("test_case", {})
        expected = case.get("generation_config", {}).get("estimated_credits")
        actual = metrics.get("credits")
        if not isinstance(expected, (int, float)) or not isinstance(actual, (int, float)):
            return self._unassessable("O5", "actual or estimated credit facts unavailable")
        ratio = actual / max(float(expected), 1.0)
        if ratio <= 1.10:
            score, verdict = 2.0, "cost_within_preview"
        elif ratio <= 1.50:
            score, verdict = 1.0, "cost_moderately_above_preview"
        else:
            score, verdict = 0.0, "cost_far_above_preview"
        return self._judgment(
            "O5",
            score,
            verdict,
            {
                "actual_credits": actual,
                "estimated_credits": expected,
                "credit_ratio": round(ratio, 4),
                "billed_seconds": metrics.get("billed_seconds"),
            },
            "实际积分与冻结计划预算比较；满意结果筛选成本在重复组报告中汇总。",
        )

    def _score_r1(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        values = [
            value
            for value in (metrics.get("connect_ms"), metrics.get("first_frame_ms"))
            if isinstance(value, (int, float))
        ]
        if not values:
            return self._unassessable("R1", "connect/first-frame timestamps unavailable")
        latency = max(values)
        if latency <= 1000:
            score, verdict = 2.0, "fast_start"
        elif latency <= 3000:
            score, verdict = 1.0, "noticeable_start_wait"
        else:
            score, verdict = 0.0, "slow_start"
        return self._judgment(
            "R1", score, verdict, {"startup_latency_ms": latency}, "使用会话建立与首帧时间戳。"
        )

    def _score_r2(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        latency = metrics.get("first_output_change_ms")
        if not isinstance(latency, (int, float)):
            return self._unassessable("R2", "no instrumented input-to-output change latency")
        if latency <= 200:
            score, verdict = 2.0, "responsive_interaction"
        elif latency <= 600:
            score, verdict = 1.0, "noticeable_interaction_latency"
        else:
            score, verdict = 0.0, "slow_interaction_response"
        return self._judgment(
            "R2", score, verdict, {"first_output_change_ms": latency}, "使用输入事件到首个可见输出变化的时钟差。"
        )

    def _score_r3(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        ratio = metrics.get("track_send_success_ratio")
        if not isinstance(ratio, (int, float)):
            return self._unassessable("R3", "no tracked interaction send facts")
        if ratio >= 0.98:
            score, verdict = 2.0, "interaction_events_delivered"
        elif ratio >= 0.85:
            score, verdict = 1.0, "some_interaction_events_lost"
        else:
            score, verdict = 0.0, "interaction_delivery_unreliable"
        return self._judgment(
            "R3",
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
            return self._unassessable("R5", "no versioned recovery perturbation was executed")
        if recovery_ms <= 500:
            score, verdict = 2.0, "fast_recovery"
        elif recovery_ms <= 2000:
            score, verdict = 1.0, "recovered_with_visible_delay"
        else:
            score, verdict = 0.0, "slow_or_failed_recovery"
        return self._judgment(
            "R5",
            score,
            verdict,
            {"recovery_ms": recovery_ms, "perturbation_count": perturbations},
            "使用版本化异常脚本的恢复时间；没有注入异常时不臆测满分。",
        )

    def _score_r6(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        duration = metrics.get("session_duration_s")
        if not isinstance(duration, (int, float)) or duration < 60:
            return self._unassessable("R6", "session shorter than the 60s shadow minimum")
        fps_cv = metrics.get("fps_window_cv")
        if not isinstance(fps_cv, (int, float)):
            return self._unassessable("R6", "long-session window metrics unavailable")
        if fps_cv <= 0.05:
            score, verdict = 2.0, "stable_long_session_runtime"
        elif fps_cv <= 0.15:
            score, verdict = 1.0, "minor_long_session_degradation"
        else:
            score, verdict = 0.0, "long_session_runtime_degraded"
        return self._judgment(
            "R6",
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
            return self._unassessable("R7", "captured output FPS unavailable")
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
            score,
            verdict,
            {"fps": fps, "frames_captured": frames, "dropped_frames": dropped, "drop_ratio": drop_ratio},
            "使用浏览器输出帧时间戳；拖影和重影由视觉Judge补充。",
        )

    def _judgment(
        self,
        dimension: str,
        score: float,
        verdict: str,
        metrics: dict[str, Any],
        description: str,
    ) -> dict[str, Any]:
        return {
            "dimension_id": dimension,
            "verdict": verdict,
            "score": score,
            "confidence": 0.95,
            "assessable": True,
            "evidence": [{"description": description}],
            "raw_metrics": metrics,
        }

    @staticmethod
    def _unassessable(dimension: str, reason: str) -> dict[str, Any]:
        return {
            "dimension_id": dimension,
            "verdict": "unassessable",
            "score": None,
            "confidence": None,
            "assessable": False,
            "evidence": [{"description": reason}],
            "raw_metrics": {},
        }


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
    except (OSError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
