"""Deterministic video-quality CV signals.

The default backend decodes a low-resolution grayscale stream with ffmpeg and
measures sharpness, exposure clipping, flicker and duplicate frames.  These
signals are deliberately limited to C9/O6; semantic or structural judgments
remain with the MLLM/specialized CV routes.
"""

from __future__ import annotations

import subprocess
from typing import Any


class VideoQualityJudge:
    def __init__(self, backend: Any = None) -> None:
        self._backend = backend

    def manifest(self) -> dict[str, Any]:
        return {
            "judge_id": "video-quality-cv",
            "version": "0.3.0-criterion-shadow",
            "kind": "cv",
            "supported_dimensions": ["C9", "O6"],
            "supported_criteria": ["C9.1", "C9.2", "O6.1"],
            "supported_modes": ["offline", "realtime"],
            "required_inputs": ["result_video"],
            "entrypoint": "xmax_test.judges.plugins.video_quality:VideoQualityJudge",
            "resource": {"gpu": False, "memory_mb": 512},
            "timeout_seconds": 300,
        }

    def evaluate(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        dimension_id = context.get("dimension_id", "C9")
        if self._backend is not None:
            return self._backend.evaluate(context)
        result_path = context.get("asset_paths", {}).get("result_video")
        if not result_path:
            return [self._unassessable(dimension_id, "result video path missing")]
        try:
            metrics = _measure_video(result_path)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return [self._unassessable(dimension_id, f"video CV decode failed: {exc}")]
        score, verdict = _quality_score(metrics)
        if dimension_id == "C9":
            criterion_scores = [
                ("C9.1", *_spatial_quality_score(metrics)),
                ("C9.2", *_temporal_quality_score(metrics)),
            ]
        else:
            criterion_scores = [("O6.1", score, verdict)]
        evidence = {
            "description": (
                "ffmpeg逐帧CV信号："
                f"清晰度={metrics['sharpness']:.2f}，"
                f"曝光裁切={metrics['clipped_ratio']:.3f}，"
                f"亮度闪烁={metrics['flicker']:.2f}，"
                f"疑似重复帧={metrics['duplicate_ratio']:.3f}。"
            )
        }
        criterion_results = [
            {
                "criterion_id": criterion_id,
                "verdict": criterion_verdict,
                "score": criterion_score,
                "confidence": 0.78,
                "assessable": True,
                "evidence": [evidence],
                "raw_metrics": metrics,
            }
            for criterion_id, criterion_score, criterion_verdict in criterion_scores
        ]
        return [
            {
                "dimension_id": dimension_id,
                "verdict": verdict,
                "score": sum(item["score"] for item in criterion_results) / len(criterion_results),
                "confidence": 0.78,
                "assessable": True,
                "evidence": [evidence],
                "raw_metrics": metrics,
                "criterion_results": criterion_results,
            }
        ]

    @staticmethod
    def _unassessable(dimension_id: str, reason: str) -> dict[str, Any]:
        criterion_ids = ["C9.1", "C9.2"] if dimension_id == "C9" else ["O6.1"]
        evidence = [{"description": reason}]
        return {
            "dimension_id": dimension_id,
            "verdict": "unassessable",
            "score": None,
            "confidence": None,
            "assessable": False,
            "evidence": evidence,
            "raw_metrics": {},
            "criterion_results": [
                {
                    "criterion_id": criterion_id,
                    "verdict": "unassessable",
                    "score": None,
                    "confidence": None,
                    "assessable": False,
                    "evidence": evidence,
                    "raw_metrics": {},
                }
                for criterion_id in criterion_ids
            ],
        }


def _measure_video(path: str, *, width: int = 160, height: int = 90) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            f"fps=2,scale={width}:{height}:flags=area,format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        capture_output=True,
        timeout=300,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace")[-500:])
    frame_size = width * height
    frames = [
        completed.stdout[offset : offset + frame_size]
        for offset in range(0, len(completed.stdout), frame_size)
        if len(completed.stdout[offset : offset + frame_size]) == frame_size
    ]
    if not frames:
        raise RuntimeError("no decodable frames")
    means: list[float] = []
    sharpness_values: list[float] = []
    clipped = 0
    total = 0
    duplicate_count = 0
    previous: bytes | None = None
    for frame in frames:
        values = list(frame)
        total += len(values)
        clipped += sum(1 for value in values if value <= 5 or value >= 250)
        means.append(sum(values) / len(values))
        horizontal = sum(
            abs(values[index] - values[index - 1])
            for index in range(1, len(values))
            if index % width
        )
        vertical = sum(
            abs(values[index] - values[index - width]) for index in range(width, len(values))
        )
        sharpness_values.append((horizontal + vertical) / (2 * len(values)))
        if previous is not None:
            mae = sum(abs(a - b) for a, b in zip(frame, previous)) / frame_size
            if mae < 0.75:
                duplicate_count += 1
        previous = frame
    flicker = sum(abs(right - left) for left, right in zip(means, means[1:])) / max(
        1, len(means) - 1
    )
    return {
        "sample_fps": 2,
        "sampled_frames": len(frames),
        "sharpness": round(sum(sharpness_values) / len(sharpness_values), 4),
        "clipped_ratio": round(clipped / max(total, 1), 6),
        "flicker": round(flicker, 4),
        "duplicate_ratio": round(duplicate_count / max(1, len(frames) - 1), 6),
    }


def _quality_score(metrics: dict[str, Any]) -> tuple[float, str]:
    severe = (
        metrics["sharpness"] < 2.5
        or metrics["clipped_ratio"] > 0.35
        or metrics["duplicate_ratio"] > 0.50
        or metrics["flicker"] > 40
    )
    if severe:
        return 0.0, "severe_perceptual_quality_issue"
    minor = (
        metrics["sharpness"] < 5.0
        or metrics["clipped_ratio"] > 0.15
        or metrics["duplicate_ratio"] > 0.20
        or metrics["flicker"] > 20
    )
    if minor:
        return 1.0, "minor_perceptual_quality_issue"
    return 2.0, "stable_basic_perceptual_quality"


def _spatial_quality_score(metrics: dict[str, Any]) -> tuple[float, str]:
    if metrics["sharpness"] < 2.5 or metrics["clipped_ratio"] > 0.35:
        return 0.0, "severe_spatial_quality_issue"
    if metrics["sharpness"] < 5.0 or metrics["clipped_ratio"] > 0.15:
        return 1.0, "minor_spatial_quality_issue"
    return 2.0, "stable_spatial_quality"


def _temporal_quality_score(metrics: dict[str, Any]) -> tuple[float, str]:
    if metrics["duplicate_ratio"] > 0.50 or metrics["flicker"] > 40:
        return 0.0, "severe_temporal_quality_issue"
    if metrics["duplicate_ratio"] > 0.20 or metrics["flicker"] > 20:
        return 1.0, "minor_temporal_quality_issue"
    return 2.0, "stable_temporal_quality"
