import unittest
from copy import deepcopy
from pathlib import Path

from xmax_test.benchmark import load_benchmark_contract
from xmax_test.evaluation.weights import resolve_scene_weights
from xmax_test.scenarios import (
    ScenarioPackError,
    load_scenario_pack,
    validate_benchmark_scenario_references,
)


class ScenarioPackTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.benchmark = load_benchmark_contract(root / "BENCHMARK.md")
        self.pack = load_scenario_pack(root / "config" / "scenarios.json")

    def test_current_rules_reference_valid_scenarios_and_modes(self) -> None:
        validate_benchmark_scenario_references(self.benchmark, self.pack)

    def test_all_shadow_scene_rules_reproduce_declared_percentages(self) -> None:
        profiles = {item["profile_id"]: item for item in self.benchmark["weight_profiles"]}
        rules = self.benchmark["scene_weight_rules"]
        for rule in rules:
            with self.subTest(rule_id=rule["rule_id"]):
                conditions = rule["when"]["all"]
                scenario_id = next(
                    item["scenario_id"] for item in conditions if "scenario_id" in item
                )
                mode = next(item["mode"] for item in conditions if "mode" in item)
                profile = profiles[rule["applicable_profile_ids"][0]]
                result = resolve_scene_weights(
                    profile,
                    rules,
                    mode=mode,
                    scenario_id=scenario_id,
                    scene_tags={},
                    include_shadow_rules=True,
                )
                expected = {
                    key: value / 100 for key, value in sorted(rule["weight_overrides"].items())
                }
                self.assertEqual(result.effective_weights, expected)
                self.assertEqual(
                    result.rule_excluded_dimensions,
                    tuple(sorted(rule.get("exclude_dimensions", []))),
                )

    def test_unknown_scenario_reference_is_rejected(self) -> None:
        benchmark = deepcopy(self.benchmark)
        benchmark["scene_weight_rules"][0]["when"]["all"][0]["scenario_id"] = "missing-scene"
        with self.assertRaises(ScenarioPackError):
            validate_benchmark_scenario_references(benchmark, self.pack)

    def test_mode_not_supported_by_scene_is_rejected(self) -> None:
        benchmark = deepcopy(self.benchmark)
        rule = next(
            item
            for item in benchmark["scene_weight_rules"]
            if item["rule_id"] == "core-travel-vlog-scene-style-offline"
        )
        rule["when"]["all"][1]["mode"] = "realtime"
        with self.assertRaises(ScenarioPackError):
            validate_benchmark_scenario_references(benchmark, self.pack)


if __name__ == "__main__":
    unittest.main()
