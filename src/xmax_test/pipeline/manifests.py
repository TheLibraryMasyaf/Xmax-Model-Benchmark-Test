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
from ..errors import ContractError
from ..hashing import content_hash
from ..time import utc_now

MANIFEST_VERSION = "1.0"
PRODUCER_VERSION = "0.3.0"


def frozen_run_batch(repository: Any, run_batch_id: str) -> list[dict[str, Any]]:
    """Return only the immutable Run members named by a Run Batch Manifest."""

    return _frozen_batch_items(
        repository,
        entity_type="run_batch",
        batch_id=run_batch_id,
        getter=repository.get_run,
        item_id_field="run_id",
        # A completed Run may be referenced by a later resume batch.  The
        # Manifest is the membership contract; generation_runs.run_batch_id is
        # only the Run's creation-batch provenance and must not be rewritten.
        binding_field=None,
    )


def frozen_evaluation_batch(
    repository: Any, evaluation_batch_id: str
) -> list[dict[str, Any]]:
    """Return only the immutable EvaluationResult members named by its Manifest."""

    items = _frozen_batch_items(
        repository,
        entity_type="evaluation_batch",
        batch_id=evaluation_batch_id,
        getter=repository.get_evaluation_result,
        item_id_field="evaluation_id",
        # Resume batches may reuse an already completed EvaluationResult and
        # then recompute only the group context. Membership is defined by the
        # immutable Manifest, not by rewriting the result's origin batch.
        binding_field=None,
    )
    run_ids = [str(item.get("run_id") or "") for item in items]
    if not all(run_ids) or len(run_ids) != len(set(run_ids)):
        raise ContractError(
            f"evaluation_batch manifest {evaluation_batch_id} must contain exactly "
            "one EvaluationResult per non-empty Run ID"
        )
    return items


def collision_safe_batch_manifest(
    repository: Any, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Version a batch identity when the requested ID is already frozen.

    A partial generation/evaluation attempt may freeze a smaller member set.
    Resume must keep that Manifest immutable, so a changed member set receives
    a deterministic content-hash suffix instead of being silently ignored or
    overwriting history.
    """

    existing_by_id = {
        item["batch_id"]: item
        for item in repository.list_batch_manifests(manifest.get("entity_type"))
    }
    batch_id = str(manifest.get("batch_id") or "")
    existing = existing_by_id.get(batch_id)
    if existing is None or existing.get("content_hash") == manifest.get("content_hash"):
        return manifest
    digest = str(manifest.get("content_hash") or "")
    for length in (8, 12, 16, 24, 32, 64):
        candidate_id = f"{batch_id}-{digest[:length]}"
        candidate = existing_by_id.get(candidate_id)
        if candidate is None or candidate.get("content_hash") == digest:
            return {
                **manifest,
                "batch_id": candidate_id,
                "metadata": {
                    **manifest.get("metadata", {}),
                    "resumed_from_frozen_batch_id": batch_id,
                },
            }
    raise ContractError(
        f"cannot allocate collision-safe identity for {manifest.get('entity_type')}/{batch_id}"
    )


def _frozen_batch_items(
    repository: Any,
    *,
    entity_type: str,
    batch_id: str,
    getter: Any,
    item_id_field: str,
    binding_field: str | None,
) -> list[dict[str, Any]]:
    manifest = repository.get_batch_manifest(entity_type, batch_id)
    item_ids = list(manifest.get("item_ids", []))
    if len(item_ids) != len(set(item_ids)):
        raise ContractError(f"{entity_type} manifest {batch_id} contains duplicate item IDs")
    items = [getter(item_id) for item_id in item_ids]
    mismatched_ids = [
        str(item.get(item_id_field) or "")
        for item in items
        if binding_field is not None and item.get(binding_field) != batch_id
    ]
    if mismatched_ids:
        raise ContractError(
            f"{entity_type} manifest {batch_id} contains items bound to another batch: "
            + ", ".join(mismatched_ids)
        )
    return items


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
