"""Selector resolution and freezing.

A request selector (``state=request``) is resolved to a frozen snapshot of
stable entity IDs before any stage runs, so later execution never drifts with
remote data.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contracts import EntityRef
from ..errors import ContractError, MissingInputError, NotFoundError
from ..hashing import content_hash
from ..time import utc_now


class SelectorResolver:
    def __init__(self, repository: Any, manifest_root: Path | None = None) -> None:
        self._repository = repository
        self._manifest_root = manifest_root

    def resolve(self, selector: dict[str, Any]) -> dict[str, Any]:
        selector_id = selector.get("selector_id")
        entity_type = selector.get("entity_type")
        state = selector.get("state", "request")
        if not selector_id or not entity_type:
            raise ContractError("selector requires selector_id and entity_type")

        if state == "frozen":
            return self._validate_frozen(selector)

        match = selector.get("match", {})
        if "ids" in match:
            ids = match["ids"]
            self._assert_exist(entity_type, ids)
        elif "filters" in match:
            ids = self._filter(entity_type, match["filters"])
        elif "manifest_uri" in match:
            ids = self._from_manifest(match["manifest_uri"])
        else:
            raise ContractError(
                f"selector {selector_id} requires match.ids, match.filters or match.manifest_uri"
            )

        if not ids:
            raise MissingInputError(
                f"selector {selector_id} resolved to an empty set of {entity_type}",
                entity_id=selector_id,
            )

        resolved: dict[str, Any] = {
            "selector_id": selector_id,
            "state": "frozen",
            "entity_type": entity_type,
            "match": match,
            "resolved_entity_ids": sorted(set(ids)),
            "snapshot_at": utc_now(),
            "snapshot_hash": content_hash({"entity_type": entity_type, "ids": sorted(set(ids))}),
        }
        self._repository.save_selector_snapshot(resolved)
        return resolved

    def _assert_exist(self, entity_type: str, ids: list[str]) -> None:
        for entity_id in ids:
            try:
                self._repository.get_batch_manifest(entity_type, entity_id)
            except NotFoundError:
                # ``test_plan`` entities are stored in the test_plans table.
                if entity_type == "test_plan":
                    try:
                        self._repository.get_test_plan(entity_id)
                        continue
                    except NotFoundError:
                        pass
                raise MissingInputError(
                    f"selector references unknown {entity_type}: {entity_id}",
                    entity_id=entity_id,
                )

    def _filter(self, entity_type: str, filters: dict[str, Any]) -> list[str]:
        if entity_type == "run_batch":
            return self._filter_run_batches(filters)
        if entity_type == "asset_batch":
            return self._filter_asset_batches(filters)
        if entity_type == "evaluation_batch":
            return self._filter_evaluation_batches(filters)
        if entity_type == "source_record":
            return []
        manifests = self._repository.list_batch_manifests(entity_type)
        ids: list[str] = []
        for manifest in manifests:
            if _matches_filters(manifest.get("metadata", {}), filters):
                ids.append(manifest["batch_id"])
        return ids

    def _filter_run_batches(self, filters: dict[str, Any]) -> list[str]:
        runs = self._repository.list_runs(
            model_id=filters.get("model_version"),
            status=filters.get("status"),
        )
        return sorted({run["run_batch_id"] for run in runs})

    def _filter_asset_batches(self, filters: dict[str, Any]) -> list[str]:
        assets = self._repository.list_assets(
            status=filters.get("status"), kind=filters.get("kind")
        )
        return sorted({f"asset-batch-{asset['asset_id']}" for asset in assets})

    def _filter_evaluation_batches(self, filters: dict[str, Any]) -> list[str]:
        results = self._repository.list_evaluation_results(
            run_id=filters.get("run_id"),
            evaluation_batch_id=filters.get("evaluation_batch_id"),
        )
        return sorted({item["evaluation_batch_id"] for item in results})

    def _from_manifest(self, manifest_uri: str) -> list[str]:
        if not self._manifest_root or not manifest_uri.startswith("manifest://"):
            raise ContractError(f"unsupported manifest_uri: {manifest_uri}")
        relative = manifest_uri[len("manifest://") :]
        path = self._manifest_root / relative
        if not path.is_file():
            raise MissingInputError(f"manifest file missing: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("item_ids", []))

    @staticmethod
    def _validate_frozen(selector: dict[str, Any]) -> dict[str, Any]:
        for key in ("resolved_entity_ids", "snapshot_at", "snapshot_hash"):
            if not selector.get(key):
                raise ContractError(f"frozen selector missing {key}")
        return selector


def _matches_filters(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    return all(metadata.get(key) == value for key, value in filters.items())


def frozen_refs(selector: dict[str, Any]) -> tuple[EntityRef, ...]:
    """Convert a frozen selector into EntityRefs for a stage manifest."""

    entity_type = selector.get("entity_type")
    refs = []
    for entity_id in selector.get("resolved_entity_ids", []):
        refs.append(
            EntityRef(
                entity_type=entity_type,
                entity_id=entity_id,
                content_hash=selector.get("snapshot_hash", ""),
            )
        )
    return tuple(refs)
