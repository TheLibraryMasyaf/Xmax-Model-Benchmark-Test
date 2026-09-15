"""One-shot context coverage tests."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from xmax_test.context import ContextChecker, _judge_coverage


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


class ProviderCredentialTests(unittest.TestCase):
    def test_decart_generation_requires_decart_not_xmax_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"DECART_API_KEY": "test-only"}, clear=True
        ):
            checker = ContextChecker(Path(directory))
            checker._check_keys(
                {
                    "generation_provider": "decart",
                    "generation_modes": ["offline"],
                    "dry_run": False,
                },
                ["generate"],
            )
            self.assertTrue(checker.summary()["ok"])

    def test_missing_decart_key_names_the_configured_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {}, clear=True
        ):
            checker = ContextChecker(Path(directory))
            checker._check_keys(
                {"generation_provider": "decart", "dry_run": False}, ["generate"]
            )
            errors = checker.summary()["errors"]
            self.assertEqual(len(errors), 1)
            self.assertIn("DECART_API_KEY", errors[0]["message"])


if __name__ == "__main__":
    unittest.main()
