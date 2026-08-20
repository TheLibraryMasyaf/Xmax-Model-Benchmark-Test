"""Stable cross-module data contracts.

The benchmark dimensions are intentionally strings. They are loaded from
BENCHMARK.md and must not be represented by a fixed Enum in application code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GenerationMode(StrEnum):
    OFFLINE = "offline"
    REALTIME = "realtime"


class RunStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


class RunOrigin(StrEnum):
    XMAX_OFFLINE = "xmax_offline"
    XMAX_REALTIME = "xmax_realtime"
    FEISHU_IMPORT = "feishu_import"
    LOCAL_IMPORT = "local_import"
    STAGE_MANIFEST_IMPORT = "stage_manifest_import"


class PipelineStage(StrEnum):
    INGEST = "ingest"
    PLAN = "plan"
    GENERATE = "generate"
    PREPROCESS = "preprocess"
    EVALUATE = "evaluate"
    FEEDBACK = "feedback"
    REPORT = "report"
    SYNC = "sync"
    RECONCILE = "reconcile"


@dataclass(frozen=True)
class TestCase:
    case_id: str
    case_number: str
    feed_number: str
    prompt_number: str
    feed_asset_id: str
    prompt_asset_ids: tuple[str, ...]
    prompt_text: str
    generation_mode: GenerationMode
    repeat_index: int
    operation_recipe_id: str
    operation_recipe_version: str
    edited_video_asset_id: str
    expected_audio_source_asset_id: str
    scenario_id: str | None = None
    scenario_pack_version: str | None = None
    scene_tags: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationRun:
    run_id: str
    run_batch_id: str
    case_id: str
    case_number: str
    status: RunStatus
    model_id: str
    mode: GenerationMode
    origin: RunOrigin
    provenance: dict[str, Any]
    result_asset_id: str | None = None
    edited_video_asset_id: str | None = None
    expected_audio_source_asset_id: str | None = None
    raw_events_uri: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Judgment:
    evaluation_id: str
    run_id: str
    benchmark_version: str
    dimension_id: str
    dimension_version: str
    judge_id: str
    judge_version: str
    verdict: str
    score: float | None
    confidence: float | None
    evidence: tuple[dict[str, Any], ...] = ()
    raw_output_uri: str | None = None


@dataclass(frozen=True)
class EvaluationResult:
    evaluation_id: str
    evaluation_batch_id: str
    run_id: str
    benchmark_version: str
    scenario_pack_version: str
    score_schema_version: str
    dimension_results: tuple[dict[str, Any], ...]
    weight_resolution: dict[str, Any]
    case_score_percent: float | None = None
    canonical_score: float | None = None
    scenario_score: float | None = None
    applied_gate_ids: tuple[str, ...] = ()
    final_verdict: str | None = None


@dataclass(frozen=True)
class HumanSignal:
    signal_id: str
    sample_id: str
    source_type: str
    raw_text: str
    normalized_labels: tuple[dict[str, Any], ...] = ()
    learning_permission: bool = False
    data_partition: str | None = None


@dataclass(frozen=True)
class EntityRef:
    entity_type: str
    entity_id: str
    content_hash: str
    manifest_uri: str | None = None


@dataclass(frozen=True)
class BatchRef:
    entity_type: str
    batch_id: str
    content_hash: str


@dataclass(frozen=True)
class BatchManifest:
    manifest_version: str
    batch_id: str
    entity_type: str
    item_entity_type: str
    item_ids: tuple[str, ...]
    content_hash: str
    producer_stage_run_id: str
    created_at: str
    source_batch_refs: tuple[BatchRef, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineSelector:
    selector_id: str
    state: str
    entity_type: str
    match: dict[str, Any]
    resolved_entity_ids: tuple[str, ...] = ()
    snapshot_at: str | None = None
    snapshot_hash: str | None = None


@dataclass(frozen=True)
class StageManifest:
    stage_run_id: str
    stage: PipelineStage
    status: str
    input_refs: tuple[EntityRef, ...]
    output_refs: tuple[EntityRef, ...]
    input_hash: str
    config_hash: str
    producer_version: str
    started_at: str
    attempt_count: int
    completed_at: str | None = None
    resumed_from_stage_run_id: str | None = None
    errors: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
