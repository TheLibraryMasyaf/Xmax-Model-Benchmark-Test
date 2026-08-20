"""Pipeline orchestration data models.

These models sit above the shared contracts in ``xmax_test.contracts`` and
describe one stage attempt, its frozen inputs and its outputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..contracts import EntityRef, PipelineStage

# Entity types produced by each stage, keyed by stage name.
STAGE_OUTPUTS: dict[str, str] = {
    "ingest": "asset_batch",
    "plan": "test_plan",
    "generate": "run_batch",
    "preprocess": "preprocess_batch",
    "evaluate": "evaluation_batch",
    "feedback": "human_signal_batch",
    "report": "report_bundle",
    "sync": "sync_batch",
    "reconcile": "sync_batch",
}

# Entity types a stage requires before it may run. ``ingest`` and ``feedback``
# consume external inputs via selectors and are not gated on pipeline outputs.
STAGE_REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    "ingest": (),
    "plan": ("asset_batch",),
    "generate": ("test_plan",),
    "preprocess": ("run_batch",),
    "evaluate": ("run_batch", "preprocess_batch"),
    "feedback": (),
    "report": ("evaluation_batch",),
    # Sync consumes the exact run_batch manifest emitted by this invocation.
    # Scores are resolved from persisted evaluation results for those runs.
    "sync": ("run_batch",),
    "reconcile": ("sync_batch",),
}

# Stable topological order: dependencies before dependents.
STAGE_ORDER: tuple[str, ...] = (
    "ingest",
    "plan",
    "generate",
    "preprocess",
    "evaluate",
    "feedback",
    "report",
    "sync",
    "reconcile",
)


@dataclass(frozen=True)
class DependencyEdge:
    producer: str
    consumer: str


def build_dag(stages: tuple[str, ...]) -> list[DependencyEdge]:
    """Return the dependency edges among the given stages."""

    edges: list[DependencyEdge] = []
    available: set[str] = set()
    for stage in stages:
        for required in STAGE_REQUIRED_INPUTS.get(stage, ()):
            producer = _producer_of(required)
            if producer in stages:
                edges.append(DependencyEdge(producer=producer, consumer=stage))
        available.add(stage)
    return edges


def _producer_of(entity_type: str) -> str | None:
    for stage, output in STAGE_OUTPUTS.items():
        if output == entity_type:
            return stage
    return None


def topological_sort(stages: list[str]) -> list[str]:
    """Stable topological sort of the authorized stages.

    ``feedback`` has no pipeline dependency and may run in parallel with the
    main line; it is placed after ``evaluate`` for deterministic reporting.
    """

    ordered = [stage for stage in STAGE_ORDER if stage in stages]
    return ordered


def required_inputs(stage: str) -> tuple[str, ...]:
    return STAGE_REQUIRED_INPUTS.get(stage, ())


def output_entity_type(stage: str) -> str:
    return STAGE_OUTPUTS.get(stage, "")


@dataclass(frozen=True)
class StageExecutionRequest:
    """Frozen input snapshot and config snapshot for one stage attempt."""

    stage_run_id: str
    stage: PipelineStage
    input_refs: tuple[EntityRef, ...]
    config_snapshot: dict[str, Any]
    dry_run: bool = False
    smoke_limit: int | None = None
    resume: bool = False


@dataclass
class StageExecutionResult:
    status: str
    output_refs: list[EntityRef] = field(default_factory=list)
    batch_manifests: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class StageExecutor(Protocol):
    """Boundary every runnable stage implements.

    An executor only consumes frozen refs/snapshots and returns a manifest
    result; it never calls other stages' services to fill missing inputs.
    """

    stage: PipelineStage

    def execute(self, request: StageExecutionRequest) -> StageExecutionResult:
        """Run the stage and return status plus output refs."""


def executor_for_stage(executors: dict[PipelineStage, Any], stage: str) -> Any:
    try:
        return executors[PipelineStage(stage)]
    except KeyError as exc:
        raise KeyError(f"no executor registered for stage {stage}") from exc
