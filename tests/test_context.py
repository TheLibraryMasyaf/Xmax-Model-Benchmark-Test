"""One-shot context coverage tests."""

from __future__ import annotations

import unittest

from xmax_test.context import _judge_coverage


class JudgeCoverageTests(unittest.TestCase):
    def benchmark(self):
        return {
            "dimensions": [
                {
                    "dimension_id": "C1",
                    "applicable_modes": ["both"],
                    "judge_routing": {
                        "primary_kinds": ["metric"],
                        "secondary_kinds": ["mlmm"],
                    },
                },
                {
                    "dimension_id": "O1",
                    "applicable_modes": ["offline"],
                    "judge_routing": {
                        "primary_kinds": ["cv"],
                        "secondary_kinds": [],
                    },
                },
            ]
        }

    def test_reports_mode_specific_gap(self) -> None:
        judges = [
            {
                "kind": "metric",
                "supported_dimensions": ["C1"],
                "supported_modes": ["offline", "realtime"],
            }
        ]
        result = _judge_coverage(self.benchmark(), judges, ["offline", "realtime"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_by_mode"]["offline"], ["O1"])
        self.assertEqual(result["missing_by_mode"]["realtime"], [])

    def test_secondary_fallback_counts_as_declared_coverage(self) -> None:
        judges = [
            {
                "kind": "mlmm",
                "supported_dimensions": ["C1"],
                "supported_modes": ["offline"],
            },
            {
                "kind": "cv",
                "supported_dimensions": ["O1"],
                "supported_modes": ["offline"],
            },
        ]
        result = _judge_coverage(self.benchmark(), judges, ["offline"])
        self.assertTrue(result["complete"])


if __name__ == "__main__":
    unittest.main()
