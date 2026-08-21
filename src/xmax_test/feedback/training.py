"""Build shadow Judge challengers from partitioned human learning packets."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

from ..errors import ContractError
from ..hashing import content_hash


class HumanLearningService:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def build_challenger(
        self,
        *,
        judge_id: str,
        version: str,
        route_kind: str,
        train_path: str | Path,
        output_directory: str | Path,
        trainer_entrypoint: str | None = None,
        base_version: str | None = None,
    ) -> dict[str, Any]:
        packets = _read_jsonl(Path(train_path))
        selected = [
            item
            for item in packets
            if item.get("route_kind") == route_kind
            and item.get("data_partition") == "train"
            and item.get("training_eligible") is True
        ]
        if not selected:
            raise ContractError(f"no Train-eligible {route_kind} packets in {train_path}")
        output = Path(output_directory)
        output.mkdir(parents=True, exist_ok=True)
        dataset_hash = content_hash(selected)
        if route_kind == "mlmm":
            artifact = output / f"{judge_id}-{version}-calibration.jsonl"
            artifact.write_text(
                "".join(
                    json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in selected
                ),
                encoding="utf-8",
            )
            training = {
                "artifact_uri": str(artifact),
                "method": "few-shot-human-calibration",
                "config_patch": {
                    "calibration": {
                        "path": str(artifact),
                        "max_examples_per_dimension": 2,
                    }
                },
            }
        elif route_kind == "cv":
            trainer = _load_trainer(trainer_entrypoint)
            training = trainer.train(str(train_path), base_version=base_version)
            if not isinstance(training, dict) or not training.get("artifact_uri"):
                raise ContractError("CV TrainerPlugin.train must return an artifact_uri")
        elif route_kind == "fusion":
            artifact = output / f"{judge_id}-{version}-fusion-supervision.jsonl"
            artifact.write_text(
                "".join(
                    json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in selected
                ),
                encoding="utf-8",
            )
            training = {
                "artifact_uri": str(artifact),
                "method": "fusion-calibration-dataset",
            }
        else:
            raise ContractError(f"unsupported learning route: {route_kind}")
        release = {
            "judge_id": judge_id,
            "version": version,
            "status": "shadow",
            "route_kind": route_kind,
            "base_version": base_version,
            "dataset_hash": dataset_hash,
            "training_sample_count": len(selected),
            "training": training,
            "validation": None,
        }
        self._repository.record_judge_release(judge_id, version, "shadow", release)
        return release


def _load_trainer(entrypoint: str | None) -> Any:
    if not entrypoint or ":" not in entrypoint:
        raise ContractError("CV learning requires --trainer-entrypoint module:object")
    module_name, object_name = entrypoint.split(":", 1)
    trainer_type = getattr(importlib.import_module(module_name), object_name)
    trainer = trainer_type()
    if not callable(getattr(trainer, "train", None)) or not callable(
        getattr(trainer, "validate", None)
    ):
        raise ContractError(
            "CV trainer must implement train(dataset_uri, base_version) and "
            "validate(artifact_uri, holdout_uri)"
        )
    return trainer


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ContractError(f"learning dataset not found: {path}")
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(item, dict):
            raise ContractError(f"learning packet at {path}:{line_number} is not an object")
        rows.append(item)
    return rows
