"""Test-plan repository facade."""

from __future__ import annotations

from typing import Any

from ..errors import NotFoundError


class PlanRepository:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def get_plan(self, plan_id: str, plan_version: str | None = None) -> dict[str, Any]:
        return self._repository.get_test_plan(plan_id, plan_version)

    def get_case(self, case_id: str) -> dict[str, Any]:
        return self._repository.get_test_case(case_id)

    def cases_of_plan(self, plan_id: str) -> list[dict[str, Any]]:
        return self._repository.list_test_cases(plan_id)

    def find_by_hash(self, plan_hash: str) -> dict[str, Any] | None:
        return self._repository.find_plan_by_hash(plan_hash)

    def save(self, plan: dict[str, Any]) -> None:
        self._repository.save_test_plan(plan)

    def max_case_suffix(self, prefix: str) -> int:
        return self._repository.max_case_suffix(prefix)
