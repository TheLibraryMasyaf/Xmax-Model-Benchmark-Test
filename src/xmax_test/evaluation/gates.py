"""Hard gate evaluation.

Hard gates cannot be offset by high scores on other dimensions. Order:
validity gate -> dimension gates -> weights -> score cap.
"""

from __future__ import annotations

from typing import Any


class HardGateEvaluator:
    def evaluate(
        self,
        benchmark: dict[str, Any],
        dimension_scores: dict[str, dict[str, Any]],
        runtime_facts: dict[str, Any],
    ) -> dict[str, Any]:
        """Evaluate all active gates and return applied gates + verdicts.

        ``dimension_scores`` maps dimension_id -> judgment dict. A blocking
        validity gate sets ``block_score`` and a final verdict.
        """

        applied: list[str] = []
        final_verdict: str | None = None
        block_score = False
        for gate in benchmark.get("hard_gates", []):
            if gate.get("status") not in {"active", "shadow"}:
                continue
            condition = gate.get("condition", {})
            action = gate.get("action", {})
            if self._matches(condition, dimension_scores, runtime_facts):
                applied.append(gate["gate_id"])
                if action.get("type") == "block_score":
                    block_score = True
                    final_verdict = action.get("final_verdict", "invalid_result")
        return {
            "applied_gate_ids": applied,
            "block_score": block_score,
            "final_verdict": final_verdict,
        }

    def _matches(
        self,
        condition: dict[str, Any],
        dimension_scores: dict[str, dict[str, Any]],
        runtime_facts: dict[str, Any],
    ) -> bool:
        dimension_id = condition.get("dimension_id")
        if dimension_id and dimension_id in dimension_scores:
            judgment = dimension_scores[dimension_id]
            if "score_equals" in condition:
                if judgment.get("score") == condition["score_equals"]:
                    return True
            if "criterion_id" in condition:
                if judgment.get("criterion_id") == condition["criterion_id"]:
                    return True
        for key, value in condition.items():
            if key in {"dimension_id", "criterion_id", "score_equals"}:
                continue
            if runtime_facts.get(key) == value:
                return True
        return False
