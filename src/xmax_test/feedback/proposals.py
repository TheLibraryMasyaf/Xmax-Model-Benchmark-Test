"""Dimension proposal lifecycle.

``proposed -> draft -> shadow -> active``. LLM may draft; only the standard
owner may activate.
"""

from __future__ import annotations

from typing import Any

from ..errors import ContractError

VALID_STATES = ("proposed", "draft", "shadow", "active")
TRANSITIONS = {
    "proposed": {"draft"},
    "draft": {"shadow", "proposed"},
    "shadow": {"active", "draft"},
    "active": {"shadow"},
}


class DimensionProposalService:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def propose(self, proposal: dict[str, Any]) -> dict[str, Any]:
        proposal_id = proposal.get("proposal_id")
        if not proposal_id:
            raise ContractError("dimension proposal requires proposal_id")
        record = {
            **proposal,
            "status": proposal.get("status", "proposed"),
            "dimension_id": proposal.get("dimension_id") or f"NEW-{proposal_id}",
        }
        if record["status"] not in VALID_STATES:
            raise ContractError(f"invalid proposal status: {record['status']!r}")
        self._repository.save_proposal(record)
        return record

    def transition(self, proposal_id: str, to_state: str) -> dict[str, Any]:
        if to_state not in VALID_STATES:
            raise ContractError(f"invalid target state: {to_state!r}")
        proposal = self._repository.get_proposal(proposal_id)
        current = proposal.get("status")
        if to_state == current:
            return proposal
        allowed = TRANSITIONS.get(current, set())
        if to_state not in allowed:
            raise ContractError(
                f"illegal transition {current} -> {to_state} for proposal {proposal_id}"
            )
        proposal["status"] = to_state
        self._repository.save_proposal(proposal)
        return proposal
