"""Deterministic scene-aware score-weight resolution.

Judges produce dimension scores. This module only resolves approved weights
from a versioned Benchmark contract; it never asks an MLLM to invent weights.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class WeightResolutionError(ValueError):
    """Raised when a weight profile or rule is ambiguous or invalid."""


@dataclass(frozen=True)
class WeightResolution:
    base_profile_id: str
    base_profile_version: str
    matched_rule_ids: tuple[str, ...]
    matched_rule_versions: tuple[str, ...]
    effective_weights: dict[str, float]
    excluded_dimensions: tuple[str, ...]
    rule_excluded_dimensions: tuple[str, ...]


def resolve_scene_weights(
    profile: Mapping[str, Any],
    rules: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    scenario_id: str | None = None,
    scene_tags: Mapping[str, Any],
    assessable_dimensions: Collection[str] | None = None,
    include_shadow_rules: bool = False,
) -> WeightResolution:
    """Resolve normalized weights for one sample.

    Rules are applied by ascending ``priority`` and then ``rule_id``. Each rule
    can multiply or override an existing profile dimension. The cumulative raw
    weight of any dimension is capped relative to its base weight by the
    profile's ``maximum_rule_multiplier``. Unassessable dimensions are removed
    before normalization.
    """

    profile_id = _required_string(profile, "profile_id", "weight profile")
    profile_version = _required_string(profile, "version", "weight profile")
    applicable_modes = profile.get("applicable_modes", [])
    if mode not in applicable_modes:
        raise WeightResolutionError(f"weight profile {profile_id} does not support mode {mode}")

    raw_weights = profile.get("weights")
    if not isinstance(raw_weights, Mapping) or not raw_weights:
        raise WeightResolutionError(f"weight profile {profile_id} has no weights")

    base: dict[str, float] = {}
    for dimension_id, value in raw_weights.items():
        if not isinstance(dimension_id, str) or not dimension_id:
            raise WeightResolutionError("weight dimension IDs must be non-empty strings")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise WeightResolutionError(f"base weight for {dimension_id} must be a positive number")
        base[dimension_id] = float(value)

    maximum_multiplier = profile.get("maximum_rule_multiplier", 2.0)
    if (
        not isinstance(maximum_multiplier, (int, float))
        or isinstance(maximum_multiplier, bool)
        or maximum_multiplier < 1
    ):
        raise WeightResolutionError("maximum_rule_multiplier must be >= 1")

    weights = dict(base)
    matched: list[str] = []
    matched_versions: list[str] = []
    rule_excluded: set[str] = set()
    allowed_statuses = {"active"}
    if include_shadow_rules:
        allowed_statuses.add("shadow")

    validated_rules: list[tuple[int, str, Mapping[str, Any]]] = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            raise WeightResolutionError("scene weight rules must be objects")
        priority = rule.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            rule_label = str(rule.get("rule_id", "<unknown>"))
            raise WeightResolutionError(f"rule {rule_label} priority must be an integer")
        validated_rules.append((priority, str(rule.get("rule_id", "")), rule))

    ordered_rules = [
        item[2] for item in sorted(validated_rules, key=lambda item: (item[0], item[1]))
    ]
    for rule in ordered_rules:
        if rule.get("status") not in allowed_statuses:
            continue
        applicable_profiles = rule.get("applicable_profile_ids")
        if (
            not isinstance(applicable_profiles, list)
            or not applicable_profiles
            or any(not isinstance(item, str) or not item for item in applicable_profiles)
        ):
            rule_label = str(rule.get("rule_id", "<unknown>"))
            raise WeightResolutionError(
                f"rule {rule_label} requires non-empty applicable_profile_ids"
            )
        if profile_id not in applicable_profiles:
            continue
        if not _rule_matches(
            rule.get("when", {}),
            mode=mode,
            scenario_id=scenario_id,
            tags=scene_tags,
        ):
            continue

        rule_id = _required_string(rule, "rule_id", "scene weight rule")
        rule_version = _required_string(rule, "version", "scene weight rule")
        matched.append(rule_id)
        matched_versions.append(rule_version)
        exclusions = rule.get("exclude_dimensions", [])
        if not isinstance(exclusions, list):
            raise WeightResolutionError(f"rule {rule_id} exclude_dimensions must be an array")
        for dimension_id in exclusions:
            _assert_known_dimension(dimension_id, base, rule_id)
            rule_excluded.add(dimension_id)
        _apply_adjustments(
            weights,
            base,
            rule.get("weight_multipliers", {}),
            rule.get("weight_overrides", {}),
            float(maximum_multiplier),
            rule_id,
        )

    if assessable_dimensions is None:
        included = set(weights) - rule_excluded
    else:
        included = set(assessable_dimensions) - rule_excluded
        unknown = included - set(weights)
        if unknown:
            raise WeightResolutionError(
                "assessable dimensions missing from weight profile: " + ", ".join(sorted(unknown))
            )

    excluded = tuple(sorted(set(weights) - included))
    selected = {key: value for key, value in weights.items() if key in included}
    total = sum(selected.values())
    if total <= 0:
        raise WeightResolutionError("no positive assessable weights remain")

    normalized = {key: value / total for key, value in sorted(selected.items())}
    return WeightResolution(
        base_profile_id=profile_id,
        base_profile_version=profile_version,
        matched_rule_ids=tuple(matched),
        matched_rule_versions=tuple(matched_versions),
        effective_weights=normalized,
        excluded_dimensions=excluded,
        rule_excluded_dimensions=tuple(sorted(rule_excluded)),
    )


def _apply_adjustments(
    weights: dict[str, float],
    base: Mapping[str, float],
    multipliers: Any,
    overrides: Any,
    maximum_multiplier: float,
    rule_id: str,
) -> None:
    if not isinstance(multipliers, Mapping) or not isinstance(overrides, Mapping):
        raise WeightResolutionError(f"rule {rule_id} adjustments must be objects")

    for dimension_id, multiplier in multipliers.items():
        _assert_known_dimension(dimension_id, base, rule_id)
        if (
            not isinstance(multiplier, (int, float))
            or isinstance(multiplier, bool)
            or multiplier <= 0
        ):
            raise WeightResolutionError(
                f"rule {rule_id} multiplier for {dimension_id} must be positive"
            )
        weights[dimension_id] = min(
            weights[dimension_id] * float(multiplier),
            base[dimension_id] * maximum_multiplier,
        )

    for dimension_id, value in overrides.items():
        _assert_known_dimension(dimension_id, base, rule_id)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise WeightResolutionError(
                f"rule {rule_id} override for {dimension_id} must be positive"
            )
        weights[dimension_id] = min(float(value), base[dimension_id] * maximum_multiplier)


def _assert_known_dimension(dimension_id: Any, base: Mapping[str, float], rule_id: str) -> None:
    if dimension_id not in base:
        raise WeightResolutionError(
            f"rule {rule_id} references dimension not in base profile: {dimension_id}"
        )


def _rule_matches(
    when: Any,
    *,
    mode: str,
    scenario_id: str | None,
    tags: Mapping[str, Any],
) -> bool:
    if not isinstance(when, Mapping):
        raise WeightResolutionError("rule when must be an object")
    all_conditions = when.get("all", [])
    any_conditions = when.get("any", [])
    if not isinstance(all_conditions, list) or not isinstance(any_conditions, list):
        raise WeightResolutionError("rule when.all and when.any must be arrays")

    all_match = all(
        _condition_matches(item, mode=mode, scenario_id=scenario_id, tags=tags)
        for item in all_conditions
    )
    any_match = (
        True
        if not any_conditions
        else any(
            _condition_matches(item, mode=mode, scenario_id=scenario_id, tags=tags)
            for item in any_conditions
        )
    )
    return all_match and any_match


def _condition_matches(
    condition: Any,
    *,
    mode: str,
    scenario_id: str | None,
    tags: Mapping[str, Any],
) -> bool:
    if not isinstance(condition, Mapping):
        raise WeightResolutionError("scene rule condition must be an object")

    if "mode" in condition:
        expected_mode = condition["mode"]
        if isinstance(expected_mode, list):
            return mode in expected_mode
        return mode == expected_mode

    if "scenario_id" in condition:
        return scenario_id == condition["scenario_id"]

    tag_name = condition.get("tag")
    if not isinstance(tag_name, str) or not tag_name:
        raise WeightResolutionError("scene rule condition requires mode, scenario_id, or tag")
    actual = tags.get(tag_name)
    if "equals" in condition:
        return actual == condition["equals"]
    if "in" in condition:
        candidates = condition["in"]
        if not isinstance(candidates, list):
            raise WeightResolutionError("scene rule condition.in must be an array")
        return actual in candidates
    raise WeightResolutionError("tag condition requires equals or in")


def _required_string(item: Mapping[str, Any], key: str, label: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise WeightResolutionError(f"{label} requires {key}")
    return value
