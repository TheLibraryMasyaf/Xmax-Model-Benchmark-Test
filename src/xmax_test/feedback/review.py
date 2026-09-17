"""Human review gate for MLLM-produced calibration mappings."""

from __future__ import annotations

from typing import Any

from ..errors import ContractError
from ..time import utc_now


class NormalizationReviewService:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def apply(self, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        approved = 0
        rejected = 0
        errors: list[dict[str, Any]] = []
        for index, decision in enumerate(decisions):
            try:
                signal_id = str(decision.get("signal_id") or "")
                outcome = str(decision.get("decision") or "")
                reviewer = str(decision.get("reviewer") or "")
                if not signal_id or outcome not in {"approved", "rejected"} or not reviewer:
                    raise ContractError(
                        "normalization review requires signal_id, reviewer and "
                        "decision=approved|rejected"
                    )
                signal = self._repository.get_human_signal(signal_id)
                if not signal.get("normalizer_id"):
                    raise ContractError(f"signal {signal_id} has not been normalized")
                reviewed_at = utc_now()
                reviewed_labels = self._reviewed_labels(
                    signal,
                    decision,
                    reviewer=reviewer,
                    reviewed_at=reviewed_at,
                )
                reviewed_comparison = self._reviewed_comparison(signal, decision)
                can_learn = (
                    outcome == "approved"
                    and signal.get("mapping_status") in {"existing", "partial"}
                    and bool(reviewed_labels)
                )
                updated = {
                    **signal,
                    "reviewed_normalized_labels": reviewed_labels if can_learn else [],
                    "reviewed_comparison": reviewed_comparison if can_learn else None,
                    "normalization_review": {
                        "status": outcome,
                        "reviewer": reviewer,
                        "reviewed_at": reviewed_at,
                        "note": decision.get("note"),
                        "scope": (
                            "selected_labels"
                            if "approved_labels" in decision
                            else "full_signal"
                        ),
                        "reviewed_label_count": len(reviewed_labels) if can_learn else 0,
                        "source": decision.get("review_source"),
                    },
                    "learning_permission": can_learn,
                }
                self._repository.update_human_signal(updated)
                if outcome == "approved":
                    approved += 1
                else:
                    rejected += 1
            except Exception as exc:
                errors.append(
                    {
                        "code": "xmax.normalization_review_failed",
                        "message": f"decision {index}: {exc}",
                        "stage": "feedback",
                        "retryable": False,
                    }
                )
        return {"approved": approved, "rejected": rejected, "errors": errors}

    @staticmethod
    def _reviewed_comparison(
        signal: dict[str, Any], decision: dict[str, Any]
    ) -> dict[str, Any] | None:
        if "approved_labels" not in decision:
            return signal.get("comparison")
        approved = decision.get("approved_comparison")
        if approved is None:
            return None
        if not isinstance(approved, dict) or approved.get("relation") not in {
            "better_than",
            "worse_than",
            "equal_to",
        }:
            raise ContractError(
                "approved_comparison requires relation=better_than|worse_than|equal_to"
            )
        return {**(signal.get("comparison") or {}), **approved}

    @staticmethod
    def _reviewed_labels(
        signal: dict[str, Any],
        decision: dict[str, Any],
        *,
        reviewer: str,
        reviewed_at: str,
    ) -> list[dict[str, Any]]:
        """Return only labels explicitly covered by the review decision.

        Older review payloads omit ``approved_labels`` and retain the original
        full-signal behavior. New field-level review payloads name the approved
        criterion and score, preventing unshown qualitative labels from being
        activated merely because another label on the same signal was reviewed.
        """

        normalized = signal.get("normalized_labels", [])
        if "approved_labels" not in decision:
            return [
                {
                    **label,
                    "human_review": {
                        "status": "approved",
                        "reviewer": reviewer,
                        "reviewed_at": reviewed_at,
                        "source": decision.get("review_source"),
                        "normalized_label_index": index,
                        "original_score_hint": label.get("score_hint"),
                    },
                }
                for index, label in enumerate(normalized)
            ]
        requested = decision.get("approved_labels")
        if not isinstance(requested, list) or not requested:
            raise ContractError("approved_labels must be a non-empty list")

        approved_by_criterion: dict[str, int] = {}
        for item in requested:
            if not isinstance(item, dict):
                raise ContractError("approved label entries must be objects")
            criterion_id = item.get("criterion_id")
            score_hint = item.get("score_hint")
            if not isinstance(criterion_id, str) or not criterion_id:
                raise ContractError("approved labels require criterion_id")
            if isinstance(score_hint, bool) or score_hint not in {0, 1, 2}:
                raise ContractError(
                    f"approved label {criterion_id} requires score_hint 0, 1 or 2"
                )
            previous = approved_by_criterion.get(criterion_id)
            if previous is not None and previous != score_hint:
                raise ContractError(
                    f"conflicting approved scores for criterion {criterion_id}"
                )
            approved_by_criterion[criterion_id] = score_hint

        reviewed: list[dict[str, Any]] = []
        for criterion_id, confirmed_score in approved_by_criterion.items():
            candidates = [
                (index, label)
                for index, label in enumerate(normalized)
                if label.get("criterion_id") == criterion_id
            ]
            if not candidates:
                raise ContractError(
                    f"approved criterion {criterion_id} is absent from normalized labels"
                )
            exact = [item for item in candidates if item[1].get("score_hint") == confirmed_score]
            pool = exact or candidates
            index, selected = max(
                pool,
                key=lambda item: float(item[1].get("confidence") or 0.0),
            )
            reviewed_label = {
                **selected,
                "score_hint": confirmed_score,
                "human_review": {
                    "status": "approved",
                    "reviewer": reviewer,
                    "reviewed_at": reviewed_at,
                    "source": decision.get("review_source"),
                    "normalized_label_index": index,
                    "original_score_hint": selected.get("score_hint"),
                },
            }
            reviewed.append(reviewed_label)
        return reviewed
