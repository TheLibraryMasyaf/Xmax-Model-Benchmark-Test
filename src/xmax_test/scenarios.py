"""Load a Scenario Pack and validate Benchmark scene-rule references."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ScenarioPackError(ValueError):
    """Raised when scenarios or Benchmark references are inconsistent."""


def load_scenario_pack(path: str | Path) -> dict[str, Any]:
    pack_path = Path(path)
    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScenarioPackError(f"invalid Scenario Pack json: {exc}") from exc

    for key, expected in {
        "scenario_pack_id": str,
        "version": str,
        "tag_definitions": dict,
        "scenarios": list,
    }.items():
        if not isinstance(pack.get(key), expected):
            raise ScenarioPackError(f"Scenario Pack field {key} must be {expected.__name__}")

    scenario_ids: set[str] = set()
    for index, scenario in enumerate(pack["scenarios"]):
        if not isinstance(scenario, dict):
            raise ScenarioPackError(f"scenarios[{index}] must be an object")
        scenario_id = scenario.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ScenarioPackError(f"scenarios[{index}].scenario_id must be a non-empty string")
        if scenario_id in scenario_ids:
            raise ScenarioPackError(f"duplicate scenario_id: {scenario_id}")
        scenario_ids.add(scenario_id)
        _validate_tags(pack["tag_definitions"], scenario_id, scenario.get("tags"))
        modes = scenario.get("supported_modes", [])
        if (
            not isinstance(modes, list)
            or not modes
            or any(mode not in {"offline", "realtime"} for mode in modes)
        ):
            raise ScenarioPackError(f"scenario {scenario_id} requires valid supported_modes")
    return pack


def validate_benchmark_scenario_references(
    benchmark: dict[str, Any], scenario_pack: dict[str, Any]
) -> None:
    scenarios = {item["scenario_id"]: item for item in scenario_pack.get("scenarios", [])}
    tag_definitions = scenario_pack.get("tag_definitions", {})
    for rule in benchmark.get("scene_weight_rules", []):
        rule_id = rule.get("rule_id", "<unknown>")
        conditions = _conditions(rule.get("when", {}), rule_id)
        referenced_scenarios: set[str] = set()
        referenced_modes: set[str] = set()
        for condition in conditions:
            if "scenario_id" in condition:
                scenario_id = condition["scenario_id"]
                if scenario_id not in scenarios:
                    raise ScenarioPackError(
                        f"rule {rule_id} references unknown scenario_id: {scenario_id}"
                    )
                referenced_scenarios.add(scenario_id)
            if "mode" in condition:
                values = condition["mode"]
                referenced_modes.update(values if isinstance(values, list) else [values])
            if "tag" in condition:
                _validate_rule_tag(tag_definitions, rule_id, condition)

        if len(referenced_scenarios) == 1 and referenced_modes:
            scenario_id = next(iter(referenced_scenarios))
            unsupported = referenced_modes - set(scenarios[scenario_id].get("supported_modes", []))
            if unsupported:
                raise ScenarioPackError(
                    f"rule {rule_id} uses modes not supported by {scenario_id}: "
                    + ", ".join(sorted(unsupported))
                )


def _validate_tags(definitions: dict[str, Any], scenario_id: str, tags: Any) -> None:
    if not isinstance(tags, dict):
        raise ScenarioPackError(f"scenario {scenario_id} tags must be an object")
    for tag, value in tags.items():
        definition = definitions.get(tag)
        if not isinstance(definition, dict):
            raise ScenarioPackError(f"scenario {scenario_id} uses undefined tag: {tag}")
        values = definition.get("values", [])
        if value not in values:
            raise ScenarioPackError(f"scenario {scenario_id} has invalid {tag} value: {value}")


def _conditions(when: Any, rule_id: str) -> list[dict[str, Any]]:
    if not isinstance(when, dict):
        raise ScenarioPackError(f"rule {rule_id}.when must be an object")
    result: list[dict[str, Any]] = []
    for group in ("all", "any"):
        items = when.get(group, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ScenarioPackError(f"rule {rule_id}.when.{group} must be objects")
        result.extend(items)
    return result


def _validate_rule_tag(
    definitions: dict[str, Any], rule_id: str, condition: dict[str, Any]
) -> None:
    tag = condition["tag"]
    definition = definitions.get(tag)
    if not isinstance(definition, dict):
        raise ScenarioPackError(f"rule {rule_id} references undefined tag: {tag}")
    allowed = set(definition.get("values", []))
    values: list[Any] = []
    if "equals" in condition:
        values.append(condition["equals"])
    if "in" in condition:
        if not isinstance(condition["in"], list):
            raise ScenarioPackError(f"rule {rule_id} condition.in must be an array")
        values.extend(condition["in"])
    invalid = [value for value in values if value not in allowed]
    if invalid:
        raise ScenarioPackError(
            f"rule {rule_id} uses invalid values for {tag}: " + ", ".join(map(str, invalid))
        )
