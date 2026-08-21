"""Pipeline dependency checking.

Only checks declared stage inputs; missing inputs return errors with
suggested commands instead of silently running other stages.
"""

from __future__ import annotations

from typing import Any

from ..errors import MissingInputError
from .models import required_inputs

SUGGESTED_COMMANDS: dict[tuple[str, str], str] = {
    (
        "plan",
        "asset_batch",
    ): "xmax-test ingest assets sync --config config/asset-sources.json",
    ("generate", "test_plan"): "xmax-test plan build --request config/run-request.json",
    ("preprocess", "run_batch"): "xmax-test generate offline --plan-id <plan_id>",
    ("evaluate", "run_batch"): "xmax-test generate offline --plan-id <plan_id>",
    (
        "evaluate",
        "preprocess_batch",
    ): "xmax-test preprocess --run-batch-id <run_batch_id>",
    ("report", "evaluation_batch"): "xmax-test evaluate --run-batch-id <run_batch_id>",
    (
        "reconcile",
        "sync_batch",
    ): "xmax-test sync run --selector <selector.json> --policy <policy>",
}


def check_stage_inputs(
    stage: str,
    available: dict[str, list[Any]],
    authorized_stages: set[str],
) -> list[dict[str, Any]]:
    """Return missing-input errors for a stage.

    ``available`` maps entity types to resolved refs from this request's
    executed upstream stages plus frozen selectors. Missing inputs raise
    ``MissingInputError``-shaped items with a suggested command.
    """

    problems: list[dict[str, Any]] = []
    for entity_type in required_inputs(stage):
        if available.get(entity_type):
            continue
        producer = _producer(entity_type)
        if producer in authorized_stages:
            continue  # produced earlier in the same request
        problems.append(
            {
                "code": "xmax.missing_input",
                "message": f"stage {stage} requires {entity_type}",
                "stage": stage,
                "retryable": False,
                "entity_id": entity_type,
                "suggested_command": SUGGESTED_COMMANDS.get((stage, entity_type)),
            }
        )
    return problems


def raise_missing_inputs(
    stage: str, available: dict[str, list[Any]], authorized_stages: set[str]
) -> None:
    problems = check_stage_inputs(stage, available, authorized_stages)
    if problems:
        first = problems[0]
        raise MissingInputError(
            first["message"],
            entity_id=first.get("entity_id"),
            suggested_command=first.get("suggested_command"),
        )


def _producer(entity_type: str) -> str | None:
    from .models import STAGE_OUTPUTS

    for stage, output in STAGE_OUTPUTS.items():
        if output == entity_type:
            return stage
    return None
