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
            "version": "0.2.0-criterion",
            "kind": "metric",
            "supported_dimensions": ["O6", "R7"],
            "supported_criteria": ["O6.2", "R7.3"],
            "supported_modes": ["offline", "realtime"],
            "required_inputs": ["result_video", "expected_audio_source"],
            "entrypoint": "xmax_test.judges.plugins.audio_integrity:AudioIntegrityJudge",
            "timeout_seconds": 300,
        }

    def evaluate(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        dimension = context.get("dimension_id")
        paths = context.get("asset_paths", {})
        result_path = paths.get("result_video")
        source_path = paths.get("expected_audio_source")
        criterion_id = "O6.2" if dimension == "O6" else "R7.3"
        if which("ffmpeg") is None or not result_path or not source_path:
            return [
                self._unassessable(dimension, criterion_id, "ffmpeg or audio source path missing")
            ]
        try:
            source = self._envelope(source_path)
            result = self._envelope(result_path)
        except Exception as exc:
            return [self._unassessable(dimension, criterion_id, f"audio decode failed: {exc}")]
        if not source or not result:
            return [
                self._with_criterion(
                    {
                        "dimension_id": dimension,
                        "criterion_id": criterion_id,
                        "verdict": "audio_missing",
                        "score": 0.0,
                        "confidence": 1.0,
                        "assessable": True,
                        "evidence": [
                            {
                                "description": "expected source or result contains no decodable audio samples"
                            }
                        ],
                        "raw_metrics": {
                            "source_windows": len(source),
                            "result_windows": len(result),
                        },
                    }
                )
            ]
        lag_windows, correlation = self._best_lag(source, result, max_lag_windows=20)
        lag_s = lag_windows * self._window_ms / 1000
        duration_ratio = len(result) / len(source)
        if correlation >= 0.65 and abs(lag_s) <= 0.2 and 0.95 <= duration_ratio <= 1.05:
            score, verdict = 2.0, "audio_preserved_and_aligned"
        elif correlation >= 0.2 and 0.85 <= duration_ratio <= 1.15:
            score, verdict = 1.0, "audio_present_with_possible_offset_or_drift"
        else:
            score, verdict = 0.0, "audio_not_preserved_or_badly_desynchronized"
        return [
            self._with_criterion(
                {
                    "dimension_id": dimension,
                    "criterion_id": criterion_id,
                    "verdict": verdict,
                    "score": score,
                    "confidence": min(0.95, max(0.55, abs(correlation))),
                    "assessable": True,
                    "evidence": [
                        {
                            "description": f"audio envelope correlation={correlation:.3f}, lag={lag_s:.3f}s, duration_ratio={duration_ratio:.3f}"
                        }
                    ],
                    "raw_metrics": {
                        "envelope_correlation": round(correlation, 4),
                        "estimated_lag_s": round(lag_s, 3),
                        "duration_ratio": round(duration_ratio, 4),
                        "window_ms": self._window_ms,
                    },
                }
            )
        ]

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
            raise RuntimeError(completed.stderr.decode("utf-8", errors="replace")[-500:])
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
    def _unassessable(dimension: str, criterion_id: str, reason: str) -> dict[str, Any]:
        return AudioIntegrityJudge._with_criterion(
            {
                "dimension_id": dimension,
                "criterion_id": criterion_id,
                "verdict": "unassessable",
                "score": None,
                "confidence": None,
                "assessable": False,
                "evidence": [{"description": reason}],
                "raw_metrics": {},
            }
        )

    @staticmethod
    def _with_criterion(judgment: dict[str, Any]) -> dict[str, Any]:
        return {
            **judgment,
            "criterion_results": [
                {
                    "criterion_id": judgment["criterion_id"],
                    "verdict": judgment["verdict"],
                    "score": judgment.get("score"),
                    "confidence": judgment.get("confidence"),
                    "assessable": judgment.get("assessable", False),
                    "evidence": list(judgment.get("evidence", [])),
                    "raw_metrics": dict(judgment.get("raw_metrics", {})),
                }
            ],
        }


def _correlation(left: list[float], right: list[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_power = math.sqrt(sum((a - left_mean) ** 2 for a in left))
    right_power = math.sqrt(sum((b - right_mean) ** 2 for b in right))
    denominator = left_power * right_power
    return numerator / denominator if denominator else 0.0
