import hashlib
import tempfile
import unittest
from pathlib import Path

from xmax_test.benchmark import BenchmarkContractError, load_benchmark_contract


class BenchmarkContractTests(unittest.TestCase):
    def test_project_shadow_contract_loads(self) -> None:
        root = Path(__file__).resolve().parents[1]
        contract = load_benchmark_contract(root / "BENCHMARK.md")
        self.assertEqual(contract["status"], "shadow")
        self.assertTrue(contract["provisional"])
        self.assertEqual(len(contract["dimensions"]), 10)
        self.assertEqual(sum(len(item["criteria"]) for item in contract["dimensions"]), 22)
        self.assertEqual(len(contract["reporting_metrics"]), 4)
        self.assertEqual(len(contract["weight_profiles"]), 2)
        self.assertEqual(len(contract["scene_weight_rules"]), 16)
        self.assertTrue(
            all(item.get("implementation_base_only") for item in contract["weight_profiles"])
        )
        self.assertTrue(
            all(
                sum(item["weight_overrides"].values()) == 100
                for item in contract["scene_weight_rules"]
            )
        )
        source = root.parent / "XMAX场景化评测标准.md"
        self.assertEqual(
            contract["source_sha256"],
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )

    def test_duplicate_dimensions_are_rejected(self) -> None:
        content = """<!-- XMAX-BENCHMARK-CONTRACT:BEGIN -->
```json
{"schema_version":"1.0","benchmark_version":"x","status":"draft","dimensions":[{"dimension_id":"C1"},{"dimension_id":"C1"}],"weight_profiles":[],"scene_weight_rules":[],"hard_gates":[],"score_schemas":[],"change_log":[]}
```
<!-- XMAX-BENCHMARK-CONTRACT:END -->"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "BENCHMARK.md"
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(BenchmarkContractError):
                load_benchmark_contract(path)

    def test_rule_must_reference_dimensions_in_its_profiles(self) -> None:
        content = """<!-- XMAX-BENCHMARK-CONTRACT:BEGIN -->
```json
{"schema_version":"1.0","benchmark_version":"x","status":"draft","dimensions":[{"dimension_id":"D1"}],"weight_profiles":[{"profile_id":"p1","weights":{"D1":1}}],"scene_weight_rules":[{"rule_id":"r1","applicable_profile_ids":["p1"],"weight_multipliers":{"D2":2}}],"hard_gates":[],"score_schemas":[],"change_log":[]}
```
<!-- XMAX-BENCHMARK-CONTRACT:END -->"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "BENCHMARK.md"
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(BenchmarkContractError):
                load_benchmark_contract(path)

    def test_score_schema_profile_must_support_its_mode(self) -> None:
        content = """<!-- XMAX-BENCHMARK-CONTRACT:BEGIN -->
```json
{"schema_version":"1.0","benchmark_version":"x","status":"draft","dimensions":[{"dimension_id":"D1"}],"weight_profiles":[{"profile_id":"p1","applicable_modes":["offline"],"weights":{"D1":1}}],"scene_weight_rules":[],"hard_gates":[],"score_schemas":[{"score_schema_id":"s1","weight_profile_by_mode":{"realtime":"p1"}}],"change_log":[]}
```
<!-- XMAX-BENCHMARK-CONTRACT:END -->"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "BENCHMARK.md"
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(BenchmarkContractError):
                load_benchmark_contract(path)


if __name__ == "__main__":
    unittest.main()
