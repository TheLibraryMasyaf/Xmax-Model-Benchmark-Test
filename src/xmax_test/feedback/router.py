"""Learning router and data partitioning.

Only signals with ``learning_permission`` enter the learning pool. Partitions
are assigned by sample group (Feed/person/prompt family/scenario) so similar
generations do not leak between Train and Holdout. Holdout is never readable by
training routes.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..errors import ContractError

PARTITIONS = ("train", "calibration", "holdout", "unassigned")

ROUTE_KINDS = ("cv", "mlmm", "fusion")


class LearningRouter:
    def __init__(
        self,
        repository: Any,
        benchmark: dict[str, Any],
        partitions: dict[str, float] | None = None,
    ) -> None:
        self._repository = repository
        self._dimensions = {
            item["dimension_id"]: item for item in benchmark.get("dimensions", [])
        }
        self._partitions = partitions or {"train": 0.7, "calibration": 0.15, "holdout": 0.15}

    def route(self, signal: dict[str, Any]) -> list[dict[str, Any]]:
        """Return learning candidates for one normalized signal."""

        if signal.get("mapping_status") not in {"existing", "partial"}:
            return []
        if not signal.get("learning_permission"):
            return []
        partition = signal.get("data_partition")
        if partition == "holdout":
            raise ContractError("holdout signals must never be read by training routes")
        if partition is None:
            partition = self.assign_partition(signal)
        return self.export_packets(signal, partition=partition)

    def export_packets(
        self, signal: dict[str, Any], *, partition: str | None = None
    ) -> list[dict[str, Any]]:
        """Build versioned packets for external CV/MLLM/Fusion consumers.

        Unlike :meth:`route`, this method may package Holdout records for
        validation. Every packet states ``training_eligible`` so a Holdout
        packet cannot be mistaken for training input.
        """

        if signal.get("mapping_status") not in {"existing", "partial"}:
            return []
        if not signal.get("learning_permission"):
            return []
        partition = partition or signal.get("data_partition") or self.assign_partition(signal)
        if partition not in PARTITIONS:
            raise ContractError(f"invalid data partition: {partition!r}")
        candidates = []
        for label in signal.get("normalized_labels", []):
            route_kind = self._route_for_dimension(label.get("dimension_id", ""))
            candidates.append(
                {
                    "packet_version": "1.0",
                    "signal_id": signal["signal_id"],
                    "dimension_id": label.get("dimension_id"),
                    "route_kind": route_kind,
                    "data_partition": partition,
                    "training_eligible": partition == "train",
                    "confidence": label.get("confidence"),
                    "sample_id": signal.get("sample_id"),
                    "evaluation_id": signal.get("evaluation_id"),
                    "supervision": {
                        "source_type": signal.get("source_type"),
                        "review_context": signal.get("review_context"),
                        "raw_text": signal.get("raw_text"),
                        "label": label,
                        "annotations": signal.get("annotations", {}),
                        "human_score_percent": signal.get("human_score_percent"),
                        "human_verdict": signal.get("human_verdict"),
                    },
                    "evidence_refs": {
                        key: signal.get(key)
                        for key in (
                            "feed_asset_id",
                            "prompt_asset_ids",
                            "result_asset_id",
                            "run_id",
                            "preprocess_id",
                        )
                        if signal.get(key) is not None
                    },
                    "normalizer": {
                        "id": signal.get("normalizer_id"),
                        "version": signal.get("normalizer_version"),
                    },
                }
            )
        return candidates

    def assign_partition(self, signal: dict[str, Any]) -> str:
        """Deterministic partition by sample group to avoid leakage."""

        group = (
            signal.get("source_group_id")
            or signal.get("sample_id")
            or signal["signal_id"]
        )
        total = sum(self._partitions.values())
        digest = hashlib.sha256(str(group).encode("utf-8")).digest()
        fraction = int.from_bytes(digest[:8], "big") / float(2**64)
        cumulative = 0.0
        for partition, weight in self._partitions.items():
            cumulative += weight / total
            if fraction <= cumulative:
                return partition
        return "train"

    def _route_for_dimension(self, dimension_id: str) -> str:
        dimension = self._dimensions.get(dimension_id)
        if dimension is None:
            raise ContractError(f"cannot route unknown dimension: {dimension_id}")
        kinds = dimension.get("judge_routing", {}).get("primary_kinds", [])
        if "cv" in kinds:
            return "cv"
        if "mlmm" in kinds:
            return "mlmm"
        if "metric" in kinds or "fusion" in kinds:
            return "fusion"
        return "mlmm"
