"""Budget preview.

Outputs combination counts, per-mode task counts, repeats, expected uploads,
expected credit range, expected duration and skip reasons. The preview never
reads real keys, submits tasks or uploads assets.
"""

from __future__ import annotations

from typing import Any

DEFAULT_COST_CONFIG = {
    "credits_per_task": {"offline": 100, "realtime": 150},
    "credits_min_factor": 0.8,
    "credits_max_factor": 1.5,
    "seconds_per_task": {"offline": 120, "realtime": 180},
    "uploads_per_case": {"offline": 2, "realtime": 1},
}


class BudgetPreview:
    def __init__(self, cost_config: dict[str, Any] | None = None) -> None:
        self._cost = {**DEFAULT_COST_CONFIG, **(cost_config or {})}

    def preview(
        self,
        *,
        cases: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        repeat_count: int,
    ) -> dict[str, Any]:
        per_mode: dict[str, int] = {}
        per_provider: dict[str, int] = {}
        exact_credits = 0
        worst_case_credits = 0
        has_exact_credits = True
        billable_seconds = 0.0
        worst_case_billable_seconds = 0.0
        retry_enabled_cases = 0
        maximum_attempts = 1
        expected_cost_usd = 0.0
        worst_case_cost_usd = 0.0
        for case in cases:
            mode = case.get("generation_mode", "offline")
            provider = case.get("generation_provider", "xmax")
            per_mode[mode] = per_mode.get(mode, 0) + 1
            per_provider[provider] = per_provider.get(provider, 0) + 1
            estimate = case.get("generation_config", {}).get("estimated_credits")
            seconds = case.get("generation_config", {}).get("estimated_billable_duration_s")
            generation = case.get("generation_config", {})
            network_retries = (
                int(generation.get("max_network_retries", 0))
                if mode == "realtime" and generation.get("network_profile_id")
                else 0
            )
            attempts = 1 + network_retries
            if network_retries:
                retry_enabled_cases += 1
                maximum_attempts = max(maximum_attempts, attempts)
            if estimate is None:
                has_exact_credits = False
            else:
                exact_credits += int(estimate)
                worst_case_credits += int(estimate) * attempts
            if seconds is not None:
                billable_seconds += float(seconds)
                worst_case_billable_seconds += float(seconds) * attempts
            cost_usd = generation.get("estimated_cost_usd")
            if cost_usd is not None:
                expected_cost_usd += float(cost_usd)
                worst_case_cost_usd += float(cost_usd) * attempts
        credits_min = 0
        credits_max = 0
        uploads = 0
        duration = 0.0
        for mode, count in per_mode.items():
            credits = self._cost.get("credits_per_task", {}).get(mode, 0)
            credits_min += count * int(credits * self._cost.get("credits_min_factor", 1))
            credits_max += count * int(credits * self._cost.get("credits_max_factor", 1))
            uploads += count * self._cost.get("uploads_per_case", {}).get(mode, 1)
            duration += count * self._cost.get("seconds_per_task", {}).get(mode, 60)

        if has_exact_credits:
            credits_min = exact_credits
            credits_max = worst_case_credits
        return {
            "combination_count": len(cases),
            "per_mode_tasks": per_mode,
            "per_provider_tasks": per_provider,
            "repeat_count": repeat_count,
            "expected_uploads": uploads,
            "expected_credits_range": [credits_min, credits_max],
            "expected_credits": exact_credits if has_exact_credits else None,
            "expected_credits_worst_case": (
                worst_case_credits if has_exact_credits else None
            ),
            "expected_billable_seconds": round(billable_seconds, 3),
            "expected_cost_usd": round(expected_cost_usd, 4),
            "expected_cost_usd_worst_case": round(worst_case_cost_usd, 4),
            "expected_billable_seconds_worst_case": round(
                worst_case_billable_seconds, 3
            ),
            "network_retry_budget": {
                "enabled_case_count": retry_enabled_cases,
                "maximum_attempts_per_case": maximum_attempts,
                "worst_case_extra_credits": (
                    worst_case_credits - exact_credits if has_exact_credits else None
                ),
                "worst_case_extra_billable_seconds": round(
                    worst_case_billable_seconds - billable_seconds, 3
                ),
            },
            "expected_duration_s": round(duration, 1),
            "skipped": skipped,
            "cost_config_used": "defaults" if not self._cost else "provided",
        }
