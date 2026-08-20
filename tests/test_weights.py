import unittest

from xmax_test.evaluation.weights import WeightResolutionError, resolve_scene_weights


PROFILE = {
    "profile_id": "offline-default",
    "version": "1.0.0",
    "status": "active",
    "applicable_modes": ["offline"],
    "maximum_rule_multiplier": 2.0,
    "weights": {"D_ACTION": 1.0, "D_STRUCTURE": 1.0, "D_QUALITY": 2.0},
}


class SceneWeightTests(unittest.TestCase):
    def test_base_weights_are_normalized(self) -> None:
        result = resolve_scene_weights(PROFILE, [], mode="offline", scene_tags={})
        self.assertEqual(result.matched_rule_ids, ())
        self.assertEqual(result.matched_rule_versions, ())
        self.assertEqual(result.rule_excluded_dimensions, ())
        self.assertAlmostEqual(result.effective_weights["D_ACTION"], 0.25)
        self.assertAlmostEqual(result.effective_weights["D_QUALITY"], 0.5)

    def test_matching_rules_adjust_weights_deterministically(self) -> None:
        rules = [
            {
                "rule_id": "fast-motion",
                "version": "1.0.0",
                "status": "active",
                "priority": 100,
                "applicable_profile_ids": ["offline-default"],
                "when": {"all": [{"tag": "motion", "equals": "fast"}]},
                "weight_multipliers": {"D_ACTION": 2.0},
            },
            {
                "rule_id": "offline-only",
                "version": "1.0.0",
                "status": "active",
                "priority": 50,
                "applicable_profile_ids": ["offline-default"],
                "when": {"all": [{"mode": "offline"}]},
                "weight_multipliers": {"D_STRUCTURE": 1.5},
            },
        ]
        result = resolve_scene_weights(
            PROFILE, rules, mode="offline", scene_tags={"motion": "fast"}
        )
        self.assertEqual(result.matched_rule_ids, ("offline-only", "fast-motion"))
        self.assertEqual(result.matched_rule_versions, ("1.0.0", "1.0.0"))
        self.assertAlmostEqual(sum(result.effective_weights.values()), 1.0)
        self.assertGreater(
            result.effective_weights["D_ACTION"],
            result.effective_weights["D_STRUCTURE"],
        )

    def test_unassessable_dimensions_are_removed_and_renormalized(self) -> None:
        result = resolve_scene_weights(
            PROFILE,
            [],
            mode="offline",
            scene_tags={},
            assessable_dimensions={"D_ACTION", "D_STRUCTURE"},
        )
        self.assertEqual(result.excluded_dimensions, ("D_QUALITY",))
        self.assertEqual(result.effective_weights, {"D_ACTION": 0.5, "D_STRUCTURE": 0.5})

    def test_rule_cannot_reference_unknown_dimension(self) -> None:
        rules = [
            {
                "rule_id": "bad",
                "version": "1.0.0",
                "status": "active",
                "priority": 1,
                "applicable_profile_ids": ["offline-default"],
                "when": {},
                "weight_multipliers": {"D_UNKNOWN": 2.0},
            }
        ]
        with self.assertRaises(WeightResolutionError):
            resolve_scene_weights(PROFILE, rules, mode="offline", scene_tags={})

    def test_wrong_mode_is_rejected(self) -> None:
        with self.assertRaises(WeightResolutionError):
            resolve_scene_weights(PROFILE, [], mode="realtime", scene_tags={})

    def test_rule_for_another_profile_is_not_applied(self) -> None:
        rules = [
            {
                "rule_id": "realtime-only",
                "version": "1.0.0",
                "status": "active",
                "priority": 1,
                "applicable_profile_ids": ["realtime-default"],
                "when": {},
                "weight_multipliers": {"D_UNKNOWN": 2.0},
            }
        ]
        result = resolve_scene_weights(
            PROFILE, rules, mode="offline", scene_tags={}
        )
        self.assertEqual(result.matched_rule_ids, ())

    def test_invalid_priority_is_rejected_with_contract_error(self) -> None:
        rules = [
            {
                "rule_id": "bad-priority",
                "version": "1.0.0",
                "status": "active",
                "priority": "first",
                "applicable_profile_ids": ["offline-default"],
                "when": {},
            }
        ]
        with self.assertRaises(WeightResolutionError):
            resolve_scene_weights(PROFILE, rules, mode="offline", scene_tags={})

    def test_scenario_rule_can_exclude_an_inapplicable_dimension(self) -> None:
        rules = [
            {
                "rule_id": "selfie",
                "version": "1.0.0",
                "status": "active",
                "priority": 1,
                "applicable_profile_ids": ["offline-default"],
                "when": {"all": [{"scenario_id": "selfie-change"}]},
                "exclude_dimensions": ["D_QUALITY"],
            }
        ]
        result = resolve_scene_weights(
            PROFILE,
            rules,
            mode="offline",
            scenario_id="selfie-change",
            scene_tags={},
        )
        self.assertEqual(result.rule_excluded_dimensions, ("D_QUALITY",))
        self.assertEqual(result.excluded_dimensions, ("D_QUALITY",))
        self.assertEqual(result.effective_weights, {"D_ACTION": 0.5, "D_STRUCTURE": 0.5})


if __name__ == "__main__":
    unittest.main()
