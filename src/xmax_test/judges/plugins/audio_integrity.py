"""Non-ML audio preservation and synchronization evidence.

The judge compares the output audio energy envelope with the Operation Recipe
declared source video. It does not look at Feed/Prompt roles heuristically.
"""

from __future__ import annotations

import array
import math
import subprocess
from shutil import which
from typing import Any


class AudioIntegrityJudge:
    def __init__(self, *, sample_rate: int = 8000, window_ms: int = 100) -> None:
        self._sample_rate = sample_rate
        self._window_ms = window_ms

    def manifest(self) -> dict[str, Any]:
        return {
            "judge_id": "audio-integrity-metric",
            "version": "0.3.1-realtime-audio-applicability",
            "kind": "metric",
            "supported_dimensions": ["G3"],
            "supported_criteria": ["G3.1", "G3.2"],
            "supported_modes": ["offline", "realtime"],
            "required_inputs": ["result_video", "expected_audio_source"],
            "entrypoint": "xmax_test.judges.plugins.audio_integrity:AudioIntegrityJudge",
            "timeout_seconds": 300,
        }

    def evaluate(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        dimension = context.get("dimension_id")
        if self._realtime_sdk_has_no_remote_audio(context):
            return [
                self._not_applicable(
                    "G3",
                    "实时SDK已请求订阅音频，但远端输出MediaStream没有音频轨；"
                    "SDK录制结果不承担音轨保留与音画同步要求。",
                )
            ]
        paths = context.get("asset_paths", {})
        result_path = paths.get("result_video")
        source_path = paths.get("expected_audio_source")
        if not source_path:
            return [self._not_applicable("G3", "没有声明应保留的Feed或Prompt音轨。")]
        if which("ffmpeg") is None or not result_path:
            return [
                self._unassessable(dimension, ["G3.1", "G3.2"], "ffmpeg or result path missing")
            ]
        try:
            source = self._envelope(source_path)
            result = self._envelope(result_path)
        except Exception as exc:
            return [self._unassessable(dimension, ["G3.1", "G3.2"], f"audio decode failed: {exc}")]
        if not source:
            return [self._not_applicable("G3", "声明的来源视频没有可解码音轨。")]
        if not result:
            return [self._multi_result("G3", [
                self._criterion("G3.1", 0.0, "expected_audio_missing", {"source_windows": len(source), "result_windows": 0}, "应有音频但结果没有可解码音轨。"),
                self._criterion("G3.2", 0.0, "audio_timeline_missing", {"source_windows": len(source), "result_windows": 0}, "结果无音轨，无法形成应有同步关系。"),
            ])]
        lag_windows, correlation = self._best_lag(source, result, max_lag_windows=20)
        lag_s = lag_windows * self._window_ms / 1000
        duration_ratio = len(result) / len(source)
        metrics = {"envelope_correlation": round(correlation, 4), "estimated_lag_s": round(lag_s, 3), "duration_ratio": round(duration_ratio, 4), "window_ms": self._window_ms}
        if correlation >= 0.65 and 0.95 <= duration_ratio <= 1.05:
            integrity = (2.0, "expected_audio_preserved")
        elif correlation >= 0.2 and 0.85 <= duration_ratio <= 1.15:
            integrity = (1.0, "audio_present_with_minor_integrity_difference")
        else:
            integrity = (0.0, "wrong_truncated_or_polluted_audio")
        if abs(lag_s) <= 0.2 and 0.95 <= duration_ratio <= 1.05:
            sync = (2.0, "audio_timeline_aligned")
        elif abs(lag_s) <= 0.6 and 0.85 <= duration_ratio <= 1.15:
            sync = (1.0, "minor_audio_offset_or_drift")
        else:
            sync = (0.0, "severe_audio_offset_or_drift")
        description = f"音频包络相关={correlation:.3f}，时间偏移={lag_s:.3f}s，时长比={duration_ratio:.3f}。"
        return [self._multi_result("G3", [
            self._criterion("G3.1", integrity[0], integrity[1], metrics, description),
            self._criterion("G3.2", sync[0], sync[1], metrics, description),
        ])]

    @staticmethod
    def _realtime_sdk_has_no_remote_audio(context: dict[str, Any]) -> bool:
        run = context.get("run") or {}
        audio = (run.get("metrics") or {}).get("audio") or {}
        return (
            context.get("mode") == "realtime"
            and run.get("origin") == "xmax_realtime"
            and audio.get("subscribe_requested") is not False
            and audio.get("subscribe") is False
        )

    def _envelope(self, path: str) -> list[float]:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(self._sample_rate),
                "-f",
                "s16le",
                "pipe:1",
            ],
            capture_output=True,
            timeout=300,
        )
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace")[-1000:]
            if "does not contain any stream" in error or "matches no streams" in error:
                return []
            raise RuntimeError(error[-500:])
        samples = array.array("h")
        samples.frombytes(completed.stdout)
        window = max(1, round(self._sample_rate * self._window_ms / 1000))
        envelope = []
        for start in range(0, len(samples), window):
            block = samples[start : start + window]
            if block:
                envelope.append(math.sqrt(sum(value * value for value in block) / len(block)))
        return envelope

    @staticmethod
    def _best_lag(
        source: list[float], result: list[float], max_lag_windows: int
    ) -> tuple[int, float]:
        best = (0, -1.0)
        for lag in range(-max_lag_windows, max_lag_windows + 1):
            if lag >= 0:
                left, right = source[lag:], result
            else:
                left, right = source, result[-lag:]
            count = min(len(left), len(right))
            if count < 3:
                continue
            correlation = _correlation(left[:count], right[:count])
            if correlation > best[1]:
                best = (lag, correlation)
        return best

    @staticmethod
    def _unassessable(dimension: str, criterion_ids: list[str], reason: str) -> dict[str, Any]:
        return AudioIntegrityJudge._multi_result(dimension, [
            {"criterion_id": criterion_id, "verdict": "unassessable", "score": None, "confidence": None, "assessable": False, "evidence": [{"description": reason}], "raw_metrics": {}}
            for criterion_id in criterion_ids
        ])

    @staticmethod
    def _criterion(criterion_id: str, score: float, verdict: str, metrics: dict[str, Any], description: str) -> dict[str, Any]:
        return {
            "criterion_id": criterion_id, "verdict": verdict, "score": score,
            "confidence": 0.9, "assessable": True,
            "evidence": [{"description": description}], "raw_metrics": metrics,
        }

    @staticmethod
    def _multi_result(dimension: str, criteria: list[dict[str, Any]]) -> dict[str, Any]:
        scores = [float(item["score"]) for item in criteria if isinstance(item.get("score"), (int, float))]
        all_not_applicable = bool(criteria) and all(
            item.get("applicable") is False for item in criteria
        )
        verdict = (
            "criterion_scores_recorded"
            if scores
            else "not_applicable"
            if all_not_applicable
            else "unassessable"
        )
        return {"dimension_id": dimension, "verdict": verdict, "score": sum(scores) / len(scores) if scores else None, "confidence": 0.9 if scores else None, "assessable": bool(scores), "applicable": not all_not_applicable, "evidence": [entry for item in criteria for entry in item.get("evidence", [])], "raw_metrics": {}, "criterion_results": criteria}

    @staticmethod
    def _not_applicable(dimension: str, reason: str) -> dict[str, Any]:
        criteria = []
        for criterion_id in ("G3.1", "G3.2"):
            criteria.append({"criterion_id": criterion_id, "verdict": "not_applicable", "score": None, "confidence": 1.0, "assessable": False, "applicable": False, "evidence": [{"description": reason}], "raw_metrics": {}})
        return AudioIntegrityJudge._multi_result(dimension, criteria)


def _correlation(left: list[float], right: list[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_power = math.sqrt(sum((a - left_mean) ** 2 for a in left))
    right_power = math.sqrt(sum((b - right_mean) ** 2 for b in right))
    denominator = left_power * right_power
    return numerator / denominator if denominator else 0.0
