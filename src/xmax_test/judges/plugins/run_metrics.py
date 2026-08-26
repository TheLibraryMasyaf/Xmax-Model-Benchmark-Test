"""Deterministic single-Run facts for P.1, realtime G2.1 and R1.

P.2/P.3/RP.1/RP.2 are produced by BatchReportingMetrics without scores.
"""

from __future__ import annotations

from typing import Any


class RunMetricsJudge:
    VERSION = "0.4.0-pgr-shadow"

    def manifest(self) -> dict[str, Any]:
        return {
            "judge_id": "run-metrics", "version": self.VERSION, "kind": "metric",
            "supported_dimensions": ["P", "G2", "R1"],
            "supported_criteria": ["P.1", "G2.1", "R1.1", "R1.2"],
            "supported_modes": ["offline", "realtime"],
            "required_inputs": ["generation_run"],
            "entrypoint": "xmax_test.judges.plugins.run_metrics:RunMetricsJudge",
            "timeout_seconds": 30,
        }

    def evaluate(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        dimension = str(context.get("dimension_id") or "")
        method = getattr(self, f"_score_{dimension.lower()}", None)
        if method is None:
            criteria = [item["criterion_id"] for item in context.get("dimension_contract", {}).get("criteria", []) if item.get("criterion_id")]
            return [self._unassessable(dimension, criteria, "unsupported runtime dimension")]
        return [method(context)]

    def _score_p(self, context: dict[str, Any]) -> dict[str, Any]:
        run = context.get("run", {})
        valid = run.get("status") == "completed" and bool(run.get("result_asset_id"))
        return self._multi("P", [self._criterion(
            "P.1", 2.0 if valid else 0.0,
            "result_registered" if valid else "no_registered_result",
            {"completed": run.get("status") == "completed", "result_asset_registered": bool(run.get("result_asset_id"))},
            "这里只验证Run终态和结果资产登记；黑屏、错误页面和有效画面由CV/MLMM补充。",
        )])

    def _score_g2(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("mode") != "realtime":
            return self._unassessable("G2", ["G2.1"], "offline frame timing is owned by video CV")
        metrics = context.get("run", {}).get("metrics", {})
        fps, frames, dropped = metrics.get("fps"), metrics.get("frames_captured"), metrics.get("dropped_frames")
        if not isinstance(fps, (int, float)):
            return self._unassessable("G2", ["G2.1"], "captured output FPS unavailable")
        drop_ratio = float(dropped) / max(float(frames) + float(dropped), 1.0) if isinstance(frames, (int, float)) and isinstance(dropped, (int, float)) else None
        if fps >= 24 and (drop_ratio is None or drop_ratio <= 0.02):
            score, verdict = 2.0, "stable_realtime_frame_updates"
        elif fps >= 15 and (drop_ratio is None or drop_ratio <= 0.10):
            score, verdict = 1.0, "usable_realtime_frame_updates"
        else:
            score, verdict = 0.0, "low_or_unstable_realtime_frame_updates"
        return self._multi("G2", [self._criterion("G2.1", score, verdict, {"fps": fps, "frames_captured": frames, "dropped_frames": dropped, "drop_ratio": drop_ratio}, "使用输出帧到达时间与掉帧事实评价持续更新。")])

    def _score_r1(self, context: dict[str, Any]) -> dict[str, Any]:
        metrics = context.get("run", {}).get("metrics", {})
        criteria: list[dict[str, Any]] = []
        latency = metrics.get("first_output_change_ms")
        if isinstance(latency, (int, float)):
            score = 2.0 if latency <= 200 else (1.0 if latency <= 600 else 0.0)
            criteria.append(self._criterion("R1.1", score, "first_visible_change_recorded", {"first_output_change_ms": latency}, "使用输入事件到首个可见输出变化的单调时钟差。"))
        else:
            criteria.extend(self._unassessable("R1", ["R1.1"], "no instrumented input-to-output change latency")["criterion_results"])
        events = int(metrics.get("interaction_event_count") or 0)
        p95 = metrics.get("interaction_latency_p95_ms")
        slope = metrics.get("interaction_latency_slope_ms_per_event")
        peak = metrics.get("pending_event_peak")
        if events < 2:
            item = self._not_applicable("R1.2", "未执行至少两次连续交互实验。")
        elif not isinstance(p95, (int, float)):
            item = self._unassessable("R1", ["R1.2"], "continuous interaction latency series unavailable")["criterion_results"][0]
        else:
            severe = p95 > 900 or (isinstance(slope, (int, float)) and slope > 80) or (isinstance(peak, (int, float)) and peak > 3)
            minor = p95 > 400 or (isinstance(slope, (int, float)) and slope > 20) or (isinstance(peak, (int, float)) and peak > 1)
            score = 0.0 if severe else (1.0 if minor else 2.0)
            item = self._criterion("R1.2", score, "continuous_latency_recorded", {"interaction_latency_p95_ms": p95, "interaction_latency_slope_ms_per_event": slope, "pending_event_peak": peak, "events_recorded": events}, "评价连续操作P95、延迟趋势与事件堆积。")
        criteria.append(item)
        return self._multi("R1", criteria)

    @staticmethod
    def _criterion(criterion_id: str, score: float, verdict: str, metrics: dict[str, Any], description: str) -> dict[str, Any]:
        return {"criterion_id": criterion_id, "verdict": verdict, "score": score, "confidence": 0.95, "assessable": True, "applicable": True, "evidence": [{"description": description}], "raw_metrics": metrics}

    @staticmethod
    def _multi(dimension: str, criteria: list[dict[str, Any]]) -> dict[str, Any]:
        scores = [float(item["score"]) for item in criteria if isinstance(item.get("score"), (int, float))]
        return {"dimension_id": dimension, "verdict": "criterion_scores_recorded" if scores else "unassessable", "score": sum(scores) / len(scores) if scores else None, "confidence": 0.95 if scores else None, "assessable": bool(scores), "evidence": [entry for item in criteria for entry in item.get("evidence", [])], "raw_metrics": {}, "criterion_results": criteria}

    @staticmethod
    def _unassessable(dimension: str, criterion_ids: list[str], reason: str) -> dict[str, Any]:
        return RunMetricsJudge._multi(dimension, [{"criterion_id": criterion_id, "verdict": "unassessable", "score": None, "confidence": None, "assessable": False, "applicable": True, "evidence": [{"description": reason}], "raw_metrics": {}} for criterion_id in criterion_ids])

    @staticmethod
    def _not_applicable(criterion_id: str, reason: str) -> dict[str, Any]:
        return {"criterion_id": criterion_id, "verdict": "not_applicable", "score": None, "confidence": 1.0, "assessable": False, "applicable": False, "evidence": [{"description": reason}], "raw_metrics": {}}
