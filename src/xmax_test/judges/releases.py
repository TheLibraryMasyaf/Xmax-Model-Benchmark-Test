"""Judge release lifecycle.

Judge states: registered -> shadow -> champion -> deprecated. Switching the
Champion records an event and never overwrites validation reports.
"""

from __future__ import annotations

from typing import Any

from ..errors import ContractError

VALID_STATES = ("registered", "shadow", "champion", "deprecated")
TRANSITIONS = {
    "registered": {"shadow"},
    "shadow": {"champion", "registered", "deprecated"},
    "champion": {"deprecated", "shadow"},
    "deprecated": set(),
}


class JudgeReleaseService:
    def __init__(self, repository: Any, clock: Any = None) -> None:
        self._repository = repository
        self._clock = clock

    def promote(
        self, judge_id: str, version: str, validation: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Promote a judge version to Champion after validation."""

        if validation is None:
            raise ContractError(
                f"cannot promote judge {judge_id}@{version} without Holdout validation"
            )
        if not validation.get("valid"):
            raise ContractError(f"cannot promote judge {judge_id}@{version}: validation failed")
        if validation.get("data_partition") not in {None, "holdout"}:
            raise ContractError("judge promotion validation must use the holdout partition")
        current = self._repository.get_judge_release(judge_id, version)
        if current is None or current.get("status") != "shadow":
            raise ContractError(f"judge {judge_id}@{version} must exist in shadow before promotion")
        self._repository.record_judge_release(
            judge_id,
            version,
            "champion",
            {
                **current,
                "validation": validation,
                "action": "promote",
            },
        )
        return {"judge_id": judge_id, "version": version, "status": "champion"}

    def rollback(self, judge_id: str, previous_version: str, reason: str) -> dict[str, Any]:
        """Roll the Champion back to a previous version."""

        self._repository.record_judge_release(
            judge_id,
            previous_version,
            "champion",
            {"action": "rollback", "reason": reason},
        )
        return {"judge_id": judge_id, "version": previous_version, "status": "champion"}

    def deprecate(self, judge_id: str, version: str) -> dict[str, Any]:
        self._repository.record_judge_release(
            judge_id, version, "deprecated", {"action": "deprecate"}
        )
        return {"judge_id": judge_id, "version": version, "status": "deprecated"}

    def state(self, judge_id: str, version: str) -> dict[str, Any] | None:
        return self._repository.get_judge_release(judge_id, version)
