import unittest

from xmax_test.judges.registry import JudgeRegistry


class FakeJudge:
    def manifest(self):
        return {
            "judge_id": "fake",
            "version": "1.0.0",
            "supported_dimensions": ["C_TEST"],
            "supported_modes": ["offline"],
        }


class JudgeRegistryTests(unittest.TestCase):
    def test_register_and_route(self) -> None:
        registry = JudgeRegistry()
        registry.register(FakeJudge())
        matches = registry.for_dimension("C_TEST", "offline")
        self.assertEqual([item.judge_id for item in matches], ["fake"])

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = JudgeRegistry()
        registry.register(FakeJudge())
        with self.assertRaises(ValueError):
            registry.register(FakeJudge())


if __name__ == "__main__":
    unittest.main()
