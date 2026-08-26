"""Load the machine-readable contract embedded in BENCHMARK.md."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BEGIN = "<!-- XMAX-BENCHMARK-CONTRACT:BEGIN -->"
END = "<!-- XMAX-BENCHMARK-CONTRACT:END -->"


class BenchmarkContractError(ValueError):
    """Raised when BENCHMARK.md cannot be consumed safely."""


def load_benchmark_contract(path: str | Path) -> dict[str, Any]:
    benchmark_path = Path(path)
    text = benchmark_path.read_text(encoding="utf-8")
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise BenchmarkContractError("BENCHMARK contract markers must appear exactly once")

    block = text.split(BEGIN, 1)[1].split(END, 1)[0].strip()
    if not block.startswith("```json") or not block.endswith("```"):
        raise BenchmarkContractError("BENCHMARK contract must be a fenced json block")

    raw = block[len("```json") : -len("```")].strip()
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BenchmarkContractError(f"invalid BENCHMARK json: {exc}") from exc

    required = {
        "schema_version": str,
        "benchmark_version": str,
        "status": str,
        "dimensions": list,
        "weight_profiles": list,
        "scene_weight_rules": list,
        "hard_gates": list,
        "score_schemas": list,
        "change_log": list,
    }
    for key, expected_type in required.items():
        if key not in contract:
            raise BenchmarkContractError(f"missing BENCHMARK field: {key}")
        if not isinstance(contract[key], expected_type):
            raise BenchmarkContractError(f"BENCHMARK field {key} must be {expected_type.__name__}")

    dimension_ids: set[str] = set()
    for index, dimension in enumerate(contract["dimensions"]):
        if not isinstance(dimension, dict):
            raise BenchmarkContractError(f"dimensions[{index}] must be an object")
        dimension_id = dimension.get("dimension_id")
        if not isinstance(dimension_id, str) or not dimension_id:
            raise BenchmarkContractError(
                f"dimensions[{index}].dimension_id must be a non-empty string"
            )
        if dimension_id in dimension_ids:
            raise BenchmarkContractError(f"duplicate dimension_id: {dimension_id}")
        dimension_ids.add(dimension_id)

    _assert_unique_ids(contract["weight_profiles"], "profile_id", "weight_profiles")
    _assert_unique_ids(contract["scene_weight_rules"], "rule_id", "scene_weight_rules")
    _assert_unique_ids(contract["hard_gates"], "gate_id", "hard_gates")
    _assert_unique_ids(contract["score_schemas"], "score_schema_id", "score_schemas")
    if "reporting_metrics" in contract:
        if not isinstance(contract["reporting_metrics"], list):
            raise BenchmarkContractError("BENCHMARK field reporting_metrics must be list")
        _assert_unique_ids(contract["reporting_metrics"], "metric_id", "reporting_metrics")
    _validate_weight_references(contract, dimension_ids)

    return contract


def _assert_unique_ids(items: list[Any], key: str, collection: str) -> None:
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise BenchmarkContractError(f"{collection}[{index}] must be an object")
        value = item.get(key)
        if not isinstance(value, str) or not value:
            raise BenchmarkContractError(f"{collection}[{index}].{key} must be a non-empty string")
        if value in seen:
            raise BenchmarkContractError(f"duplicate {key}: {value}")
        seen.add(value)


def _validate_weight_references(contract: dict[str, Any], dimension_ids: set[str]) -> None:
    profiles: dict[str, dict[str, Any]] = {}
    for profile in contract["weight_profiles"]:
        profile_id = profile["profile_id"]
        weights = profile.get("weights")
        if not isinstance(weights, dict) or not weights:
            raise BenchmarkContractError(
                f"weight profile {profile_id} must define non-empty weights"
            )
        unknown = set(weights) - dimension_ids
        if unknown:
            raise BenchmarkContractError(
                f"weight profile {profile_id} references unknown dimensions: "
                + ", ".join(sorted(unknown))
            )
        profiles[profile_id] = profile

    for rule in contract["scene_weight_rules"]:
        rule_id = rule["rule_id"]
        profile_ids = rule.get("applicable_profile_ids")
        if not isinstance(profile_ids, list) or not profile_ids:
            raise BenchmarkContractError(
                f"scene weight rule {rule_id} requires applicable_profile_ids"
            )
        unknown_profiles = set(profile_ids) - set(profiles)
        if unknown_profiles:
            raise BenchmarkContractError(
                f"scene weight rule {rule_id} references unknown profiles: "
                + ", ".join(sorted(unknown_profiles))
            )
        adjusted_dimensions: set[str] = set()
        for field in ("weight_multipliers", "weight_overrides"):
            adjustments = rule.get(field, {})
            if not isinstance(adjustments, dict):
                raise BenchmarkContractError(
                    f"scene weight rule {rule_id}.{field} must be an object"
                )
            adjusted_dimensions.update(adjustments)
        exclusions = rule.get("exclude_dimensions", [])
        if not isinstance(exclusions, list):
            raise BenchmarkContractError(
                f"scene weight rule {rule_id}.exclude_dimensions must be an array"
            )
        adjusted_dimensions.update(exclusions)
        for profile_id in profile_ids:
            missing = adjusted_dimensions - set(profiles[profile_id]["weights"])
            if missing:
                raise BenchmarkContractError(
                    f"scene weight rule {rule_id} references dimensions absent from "
                    f"profile {profile_id}: " + ", ".join(sorted(missing))
                )

    for score_schema in contract["score_schemas"]:
        schema_id = score_schema["score_schema_id"]
        mapping = score_schema.get("weight_profile_by_mode")
        if not isinstance(mapping, dict) or not mapping:
            raise BenchmarkContractError(
                f"score schema {schema_id} requires weight_profile_by_mode"
            )
        unknown_profiles = set(mapping.values()) - set(profiles)
        if unknown_profiles:
            raise BenchmarkContractError(
                f"score schema {schema_id} references unknown profiles: "
                + ", ".join(sorted(unknown_profiles))
            )
        for mode, profile_id in mapping.items():
            if mode not in {"offline", "realtime"}:
                raise BenchmarkContractError(
                    f"score schema {schema_id} has unsupported mode: {mode}"
                )
            modes = profiles[profile_id].get("applicable_modes", [])
            if mode not in modes:
                raise BenchmarkContractError(
                    f"score schema {schema_id} maps {mode} to incompatible profile {profile_id}"
                )
