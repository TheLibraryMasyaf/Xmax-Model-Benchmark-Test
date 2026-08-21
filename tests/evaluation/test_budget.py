from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xmax_test.errors import EvaluationBudgetPausedError
from xmax_test.evaluation.budget import EvaluationBudgetGate, PaidFallbackPolicy
from xmax_test.storage.sqlite import SqliteMetadataRepository
from xmax_test.time import FixedClock

CONFIG = {
    "enabled": True,
    "budget_id": "paid-test",
    "provider_id": "openai_compatible",
    "model": "qwen3-vl-flash",
    "currency": "CNY",
    "limit_cny": 1,
    "max_reservation_cny": 0.36,
    "pricing_tiers": [
        {
            "max_input_tokens": 32000,
            "input_cny_per_million": 0.15,
            "cached_input_cny_per_million": 0.03,
            "output_cny_per_million": 1.5,
        },
        {
            "max_input_tokens": 260096,
            "input_cny_per_million": 0.6,
            "cached_input_cny_per_million": 0.12,
            "output_cny_per_million": 6.0,
        },
    ],
}


class EvaluationBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = SqliteMetadataRepository(
            Path(self.temp.name) / "db.sqlite3", clock=FixedClock()
        )
        self.policy = PaidFallbackPolicy.from_config(CONFIG)
        assert self.policy is not None
        self.gate = EvaluationBudgetGate(self.repository, self.policy)

    def tearDown(self) -> None:
        self.repository.close()
        self.temp.cleanup()

    def test_first_paid_call_requires_explicit_authorization(self) -> None:
        self.assertEqual(self.gate.status()["status"], "awaiting_authorization")
        with self.assertRaises(EvaluationBudgetPausedError):
            self.gate.reserve_paid_call()
        self.assertEqual(self.gate.status()["status"], "paused")

    def test_successful_usage_settles_reserved_cost(self) -> None:
        self.gate.authorize(operator="tester")
        reservation = self.gate.reserve_paid_call()
        state = self.gate.settle(
            reservation["reservation_id"],
            {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 200},
            },
        )
        # 800*0.15 + 200*0.03 + 100*1.5 = CNY 0.000276
        self.assertEqual(state["spent_micros"], 276)
        self.assertEqual(state["reserved_micros"], 0)

    def test_reservation_enforces_hard_limit_and_persists_pause(self) -> None:
        self.gate.authorize(operator="tester")
        first = self.gate.reserve_paid_call()
        second = self.gate.reserve_paid_call()
        self.gate.forfeit(first["reservation_id"], reason="ambiguous timeout")
        with self.assertRaises(EvaluationBudgetPausedError):
            self.gate.reserve_paid_call()
        self.assertEqual(self.gate.status()["status"], "paused")
        self.gate.release(second["reservation_id"], reason="test cleanup")


if __name__ == "__main__":
    unittest.main()
