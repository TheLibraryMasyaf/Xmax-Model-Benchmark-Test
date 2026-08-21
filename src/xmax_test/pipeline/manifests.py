"""Stage and batch manifest writers.

Every stage attempt produces a Stage Manifest validated against
``stage-manifest.schema.json``; every output batch produces a Batch Manifest
against ``batch-manifest.schema.json``. Manifests are written to the manifest
root (``manifest://`` URIs) and mirrored in the repository.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..contracts import EntityRef
from ..hashing import content_hash
from ..time import utc_now

MANIFEST_VERSION = "1.0"
PRODUCER_VERSION = "0.3.0"


def _stage_run_id(clock: Any) -> str:
    return f"stage-{uuid.uuid4().hex[:12]}"


def build_stage_manifest(
    *,
    stage: str,
    input_refs: tuple[EntityRef, ...],
    output_refs: list[EntityRef],
    input_hash: str,
    config_hash: str,
    status: str = "completed",
    started_at: str | None = None,
    attempt_count: int = 1,
    resumed_from: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    stage_run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "stage_run_id": stage_run_id or _stage_run_id(None),
        "stage": stage,
        "status": status,
        "input_refs": [ref.__dict__ for ref in input_refs],
        "output_refs": [ref.__dict__ for ref in output_refs],
        "input_hash": input_hash,
        "config_hash": config_hash,
        "producer_version": PRODUCER_VERSION,
        "started_at": started_at or utc_now(),
        "completed_at": None,
        "attempt_count": attempt_count,
        "resumed_from_stage_run_id": resumed_from,
        "errors": errors or [],
        "metadata": metadata or {},
    }


def build_batch_manifest(
    *,
    entity_type: str,
    item_entity_type: str,
    item_ids: list[str],
    producer_stage_run_id: str,
    source_batch_refs: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    content_hash_value = content_hash(
        {"entity_type": entity_type, "item_ids": sorted(set(item_ids))}
    )
    batch_id = batch_id or _stable_batch_id(entity_type, item_ids, content_hash_value)
    return {
        "manifest_version": MANIFEST_VERSION,
        "batch_id": batch_id,
        "entity_type": entity_type,
        "item_entity_type": item_entity_type,
        "item_ids": sorted(set(item_ids)),
        "content_hash": content_hash_value,
        "producer_stage_run_id": producer_stage_run_id,
        "created_at": utc_now(),
        "source_batch_refs": source_batch_refs or [],
        "metadata": metadata or {},
    }


def _stable_batch_id(entity_type: str, item_ids: list[str], content_hash_value: str) -> str:
    prefix = {
        "asset_batch": "assets",
        "test_plan": "plan",
        "task_batch": "tasks",
        "run_batch": "runs",
        "preprocess_batch": "prep",
        "evaluation_batch": "eval",
        "human_signal_batch": "signals",
        "report_bundle": "report",
        "sync_batch": "sync",
    }.get(entity_type, "batch")
    return f"{prefix}-{content_hash_value[:12]}"


class ManifestStore:
    """Persists stage/batch manifests to disk and the repository."""

    def __init__(self, manifest_root: str | Path, repository: Any) -> None:
        self._root = Path(manifest_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._repository = repository

    def save_stage_manifest(self, manifest: dict[str, Any]) -> str:
        relative = f"stages/{manifest['stage_run_id']}.json"
        self._write_manifest(relative, manifest)
        self._repository.save_stage_run(manifest)
        return self.uri(relative)

    def save_batch_manifest(self, manifest: dict[str, Any]) -> str:
        relative = f"batches/{manifest['entity_type']}/{manifest['batch_id']}.json"
        self._write_manifest(relative, manifest)
        self._repository.save_batch_manifest(manifest)
        return self.uri(relative)

    def uri(self, relative: str) -> str:
        return f"manifest://{relative}"

    def _write_manifest(self, relative: str, manifest: dict[str, Any]) -> None:
        path = self._root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


def ref_for_batch(manifest: dict[str, Any]) -> EntityRef:
    return EntityRef(
        entity_type=manifest["entity_type"],
        entity_id=manifest["batch_id"],
        content_hash=manifest["content_hash"],
        manifest_uri=f"manifest://batches/{manifest['entity_type']}/{manifest['batch_id']}.json",
    )


def ref_for_plan(plan: dict[str, Any]) -> EntityRef:
    return EntityRef(
        entity_type="test_plan",
        entity_id=plan["plan_id"],
        content_hash=plan["plan_hash"],
    )
