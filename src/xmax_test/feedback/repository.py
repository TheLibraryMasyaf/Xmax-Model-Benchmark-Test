"""Human-signal repository facade."""

from __future__ import annotations

from typing import Any

from ..errors import NotFoundError


class FeedbackRepository:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def append_signal(self, signal: dict[str, Any]) -> None:
        self._repository.append_human_signal(signal)

    def signal(self, signal_id: str) -> dict[str, Any]:
        return self._repository.get_human_signal(signal_id)

    def save_proposal(self, proposal: dict[str, Any]) -> None:
        self._repository.save_proposal(proposal)

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        return self._repository.get_proposal(proposal_id)
