"""Deterministic repeat- and transfer-group evaluation.

O4 and the retry/satisfactory-cost parts of O5 cannot be inferred from one
video.  This module consumes the frozen batch of GenerationRuns and the
already persisted per-video criterion results, then emits ordinary Judgments
that are re-fused into every completed run.  A one-sample group is explicitly
N/A rather than silently receiving a neutral or full score.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

GROUP_JUDGE_ID = "batch-group-metrics"
GROUP_JUDGE_VERSION = "1.1.0-frozen-members"
GROUP_CRITERION_SCOPES = {
    "C1.2": "batch",
    "O4.1": "repeat_group",
    "O4.2": "cross_input_group",
    "O5.2": "repeat_group",
    "O5.3": "repeat_group",
}


class BatchGroupEvaluator:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def judgments(
        self,
        runs: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        run_ids = [str(run.get("run_id") or "") for run in runs]
        if not all(run_ids) or len(run_ids) != len(set(run_ids)):
            raise ValueError("group evaluation requires unique non-empty frozen run IDs")
        result_run_ids = [str(result.get("run_id") or "") for result in results]
        if len(result_run_ids) != len(set(result_run_ids)):
            raise ValueError("group evaluation received duplicate results for one Run")
        outside = sorted(set(result_run_ids) - set(run_ids))
        if outside:
            raise ValueError(
                "group evaluation results are outside the frozen Run set: " + ", ".join(outside)
            )
        cases = {
            run["run_id"]: self._repository.get_test_case(run["case_id"])
            for run in runs
            if run.get("case_id")
        }
        results_by_run = {item["run_id"]: item for item in results}
        repeat_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        transfer_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        model_mode_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for run in runs:
            case = cases.get(run.get("run_id"))
            if not case:
                continue
            model_mode_groups[(run.get("model_id"), run.get("mode"))].append(run)
            if run.get("mode") != "offline":
                continue
            repeat_groups[self._repeat_key(run, case)].append(run)
            transfer_groups[self._transfer_key(run, case)].append(run)

        output: dict[str, list[dict[str, Any]]] = {}
        for run_id, result in results_by_run.items():
            run = next((item for item in runs if item.get("run_id") == run_id), None)
            case = cases.get(run_id)
            if run is None or case is None:
                continue
            output[run_id] = [
                self._c1_judgment(
                    result,
                    model_mode_groups[(run.get("model_id"), run.get("mode"))],
                    cases,
                    results_by_run,
                )
            ]
            if run.get("mode") != "offline":
                continue
            repeat = repeat_groups[self._repeat_key(run, case)]
            transfer = transfer_groups[self._transfer_key(run, case)]
            output[run_id].extend(
                [
                    self._o4_judgment(result, repeat, transfer, cases, results_by_run),
                    self._o5_judgment(result, repeat, results_by_run),
                ]
            )
        return output

    def _c1_judgment(
        self,
        result: dict[str, Any],
        group: list[dict[str, Any]],
        cases: dict[str, dict[str, Any]],
        results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        distinct_inputs = {
            (
                cases[item["run_id"]].get("feed_asset_id"),
                tuple(cases[item["run_id"]].get("prompt_asset_ids", [])),
                cases[item["run_id"]].get("prompt_text", ""),
            )
            for item in group
            if item.get("run_id") in cases
        }
        if len(distinct_inputs) < 2:
            criterion = self._not_applicable(
                "C1.2",
                "batch contains fewer than two distinct input combinations",
                group,
            )
        else:
            completed = [item for item in group if item.get("status") == "completed"]
            retries = sum(int(item.get("metrics", {}).get("retry_count") or 0) for item in group)
            qualities = [
                self._quality(results[item["run_id"]])
                for item in completed
                if item.get("run_id") in results
            ]
            qualities = [item for item in qualities if item is not None]
            success_ratio = len(completed) / len(group)
            spread = max(qualities) - min(qualities) if len(qualities) >= 2 else 2.0
            if success_ratio < 0.8 or retries > len(group) or spread > 0.75:
                score = 0.0
            elif success_ratio < 1.0 or retries > 0 or spread > 0.35:
                score = 1.0
            else:
                score = 2.0
            criterion = self._criterion(
                "C1.2",
                score,
                {
                    "run_count": len(group),
                    "member_run_ids": sorted(item["run_id"] for item in group),
                    "distinct_input_count": len(distinct_inputs),
                    "completed_count": len(completed),
                    "retry_count": retries,
                    "success_ratio": round(success_ratio, 4),
                    "quality_spread_0_2": round(spread, 4),
                },
                "异常、重试和跨输入稳定性按同模型同模式的冻结批次计算。",
            )
        return self._judgment(result, "C1", [criterion])

    def _o4_judgment(
        self,
        result: dict[str, Any],
        repeat: list[dict[str, Any]],
        transfer: list[dict[str, Any]],
        cases: dict[str, dict[str, Any]],
        results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        same_input = self._stability_criterion("O4.1", repeat, results)
        distinct_feeds = {
            cases[item["run_id"]].get("feed_asset_id")
            for item in transfer
            if item.get("run_id") in cases
        }
        if len(distinct_feeds) < 2:
            transfer_item = self._not_applicable(
                "O4.2", "batch contains fewer than two distinct Feed inputs", transfer
            )
        else:
            transfer_item = self._stability_criterion("O4.2", transfer, results)
        return self._judgment(result, "O4", [same_input, transfer_item])

    def _o5_judgment(
        self,
        result: dict[str, Any],
        repeat: list[dict[str, Any]],
        results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        attempts = len(repeat)
        completed = [item for item in repeat if item.get("status") == "completed"]
        failures = attempts - len(completed)
        if attempts == 0:
            retry = self._not_applicable("O5.2", "no attempts in frozen group", repeat)
        else:
            retry_score = 2.0 if failures == 0 else (1.0 if failures == 1 else 0.0)
            retry = self._criterion(
                "O5.2",
                retry_score,
                {
                    "attempt_count": attempts,
                    "member_run_ids": sorted(item["run_id"] for item in repeat),
                    "failed_attempts": failures,
                    "completed_attempts": len(completed),
                },
                "失败重试成本按同一冻结输入组的真实Run终态计算。",
            )

        qualities = [
            self._quality(results[item["run_id"]])
            for item in completed
            if item.get("run_id") in results
        ]
        qualities = [item for item in qualities if item is not None]
        satisfactory = sum(1 for item in qualities if item >= 1.0)
        credits = sum(float(item.get("metrics", {}).get("credits") or 0) for item in repeat)
        if not qualities:
            cost = self._not_applicable(
                "O5.3",
                "no fully or partially assessed completed result in repeat group",
                repeat,
            )
        else:
            attempts_per_satisfactory = attempts / max(satisfactory, 1)
            if satisfactory == 0 or attempts_per_satisfactory > 4:
                score = 0.0
            elif attempts_per_satisfactory > 2:
                score = 1.0
            else:
                score = 2.0
            cost = self._criterion(
                "O5.3",
                score,
                {
                    "attempt_count": attempts,
                    "member_run_ids": sorted(item["run_id"] for item in repeat),
                    "satisfactory_count": satisfactory,
                    "attempts_per_satisfactory": round(attempts_per_satisfactory, 4),
                    "total_credits": credits,
                },
                "满意结果筛选成本按重复组中质量达到合格线的结果数与总尝试数计算。",
            )
        return self._judgment(result, "O5", [retry, cost])

    def _stability_criterion(
        self,
        criterion_id: str,
        runs: list[dict[str, Any]],
        results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if len(runs) < 2:
            return self._not_applicable(
                criterion_id,
                "fewer than two attempts are available for group stability",
                runs,
            )
        completed = [item for item in runs if item.get("status") == "completed"]
        qualities = [
            self._quality(results[item["run_id"]])
            for item in completed
            if item.get("run_id") in results
        ]
        qualities = [item for item in qualities if item is not None]
        success_ratio = len(completed) / len(runs)
        spread = max(qualities) - min(qualities) if len(qualities) >= 2 else 2.0
        if success_ratio < 0.5 or spread > 0.75:
            score = 0.0
        elif success_ratio < 1.0 or spread > 0.35:
            score = 1.0
        else:
            score = 2.0
        return self._criterion(
            criterion_id,
            score,
            {
                "attempt_count": len(runs),
                "member_run_ids": sorted(item["run_id"] for item in runs),
                "completed_count": len(completed),
                "success_ratio": round(success_ratio, 4),
                "quality_spread_0_2": round(spread, 4),
            },
            "稳定性来自同一冻结批次的Run终态及非O4/O5细则质量分布。",
        )

    @staticmethod
    def _quality(result: dict[str, Any]) -> float | None:
        values = [
            float(item["score"])
            for item in result.get("criterion_results", [])
            if item.get("dimension_id") not in {"O4", "O5"}
            and isinstance(item.get("score"), (int, float))
        ]
        return sum(values) / len(values) if values else None

    @staticmethod
    def _repeat_key(run: dict[str, Any], case: dict[str, Any]) -> tuple[Any, ...]:
        return (
            run.get("model_id"),
            run.get("mode"),
            case.get("feed_asset_id"),
            tuple(case.get("prompt_asset_ids", [])),
            case.get("prompt_text", ""),
            case.get("operation_recipe_id"),
        )

    @staticmethod
    def _transfer_key(run: dict[str, Any], case: dict[str, Any]) -> tuple[Any, ...]:
        return (
            run.get("model_id"),
            run.get("mode"),
            tuple(case.get("prompt_asset_ids", [])),
            case.get("prompt_text", ""),
            case.get("operation_recipe_id"),
            case.get("scenario_id"),
        )

    @staticmethod
    def _criterion(
        criterion_id: str,
        score: float,
        metrics: dict[str, Any],
        description: str,
    ) -> dict[str, Any]:
        return {
            "criterion_id": criterion_id,
            "verdict": "group_metric_recorded",
            "score": score,
            "confidence": 0.95,
            "assessable": True,
            "applicable": True,
            "evidence": [{"description": description}],
            "raw_metrics": metrics,
            "aggregation_scope": BatchGroupEvaluator._aggregation_scope(criterion_id),
        }

    @staticmethod
    def _not_applicable(
        criterion_id: str, reason: str, runs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "criterion_id": criterion_id,
            "verdict": "not_applicable",
            "score": None,
            "confidence": 1.0,
            "assessable": False,
            "applicable": False,
            "evidence": [{"description": reason}],
            "raw_metrics": {
                "member_run_ids": sorted(item["run_id"] for item in runs),
            },
            "aggregation_scope": BatchGroupEvaluator._aggregation_scope(criterion_id),
        }

    @staticmethod
    def _aggregation_scope(criterion_id: str) -> str:
        return GROUP_CRITERION_SCOPES.get(criterion_id, "run")

    @staticmethod
    def _judgment(
        result: dict[str, Any], dimension_id: str, criteria: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "evaluation_id": result["evaluation_id"],
            "run_id": result["run_id"],
            "benchmark_version": result["benchmark_version"],
            "dimension_id": dimension_id,
            "dimension_version": result["benchmark_version"],
            "judge_id": GROUP_JUDGE_ID,
            "judge_version": GROUP_JUDGE_VERSION,
            "verdict": "batch_group_evaluated",
            "confidence": 0.95,
            "assessable": any(item.get("assessable") for item in criteria),
            "evidence": [evidence for item in criteria for evidence in item.get("evidence", [])],
            "criterion_results": criteria,
        }
