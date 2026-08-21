"""Stable cross-module data contracts.

The benchmark dimensions are intentionally strings. They are loaded from
BENCHMARK.md and must not be represented by a fixed Enum in application code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


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
class EntityRef:
    entity_type: str
    entity_id: str
    content_hash: str
    manifest_uri: str | None = None


@dataclass(frozen=True)
class PipelineSelector:
    selector_id: str
    state: str
    entity_type: str
    match: dict[str, Any]
    resolved_entity_ids: tuple[str, ...] = ()
    snapshot_at: str | None = None
    snapshot_hash: str | None = None
