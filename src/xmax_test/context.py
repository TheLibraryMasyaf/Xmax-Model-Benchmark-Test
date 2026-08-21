"""One-shot context check that aggregates every missing item.

Runs before every real operation. It must report all problems at once instead
of making the operator discover them stage by stage. The check never performs
external side effects: no downloads, no generation, no remote writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .benchmark import load_benchmark_contract
from .config import load_config, load_dotenv, load_json, redact, secret_file
from .errors import ErrorReport, MissingDependencyError, XmaxTestError
from .scenarios import load_scenario_pack

PIPELINE_STAGES = [
    "ingest",
    "plan",
    "generate",
    "preprocess",
    "evaluate",
    "feedback",
    "report",
    "sync",
    "reconcile",
]

BILLED_STAGES = {"generate"}


class ContextChecker:
    """Aggregates findings without raising on the first problem."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.report = ErrorReport()
        self.judge_coverage: dict[str, Any] = {}

    def check_run_request(self, request: dict[str, Any]) -> dict[str, Any]:
        stages = request.get("stages", [])
        sync_policy = request.get("sync_policy", "none")

        if "sync" in stages and sync_policy == "none":
            self.report.add_item(
                {
                    "code": "xmax.contract_error",
                    "message": "sync stage requires sync_policy != none",
                    "stage": "sync",
                    "retryable": False,
                }
            )
        if "sync" not in stages and sync_policy != "none":
            self.report.add_item(
                {
                    "code": "xmax.contract_error",
                    "message": "sync_policy != none requires an explicit sync stage",
                    "stage": "sync",
                    "retryable": False,
                }
            )
        if "report" in stages and not request.get("comparison"):
            self.report.add_item(
                {
                    "code": "xmax.contract_error",
                    "message": "report stage requires a comparison block",
                    "stage": "report",
                    "retryable": False,
                }
            )

        for stage in stages:
            selectors = request.get("stage_inputs", {}).get(stage, [])
            for selector in selectors:
                self._check_selector(stage, selector)

        self._check_benchmark(request)
        self._check_scenario_pack(request)
        self._check_operation_recipes(request)
        self._check_judges(request)
        self._check_feishu(stages)
        self._check_billed_approval(stages, request)
        self._check_report_inputs(stages, request)
        self._check_existing_results(request)
        self._check_keys(request, stages)
        self._check_local_dependencies(request, stages)
        return {"request_id": request.get("request_id"), "stages": stages}

    def _check_selector(self, stage: str, selector: dict[str, Any]) -> None:
        state = selector.get("state")
        if state == "request":
            match = selector.get("match", {})
            if not any(key in match for key in ("ids", "filters", "manifest_uri")):
                self.report.add_item(
                    {
                        "code": "xmax.contract_error",
                        "message": f"{stage} selector {selector.get('selector_id')} "
                        "must define ids, filters or manifest_uri",
                        "stage": stage,
                        "retryable": False,
                    }
                )
        elif state == "frozen":
            for key in ("resolved_entity_ids", "snapshot_at", "snapshot_hash"):
                if not selector.get(key):
                    self.report.add_item(
                        {
                            "code": "xmax.contract_error",
                            "message": f"{stage} frozen selector "
                            f"{selector.get('selector_id')} is missing {key}",
                            "stage": stage,
                            "retryable": False,
                        }
                    )
        else:
            self.report.add_item(
                {
                    "code": "xmax.contract_error",
                    "message": f"{stage} selector {selector.get('selector_id')} "
                    f"has invalid state {state!r}",
                    "stage": stage,
                    "retryable": False,
                }
            )

    def _check_benchmark(self, request: dict[str, Any]) -> None:
        path = self.root / request.get("benchmark_path", "BENCHMARK.md")
        if not path.is_file():
            self.report.add_item(
                {
                    "code": "xmax.missing_input",
                    "message": f"benchmark file missing: {path}",
                    "stage": "plan",
                    "retryable": False,
                }
            )
            return
        try:
            contract = load_benchmark_contract(path)
        except XmaxTestError as exc:
            self.report.add(exc)
            return
        if contract["status"] not in {"shadow", "active"}:
            self.report.add_item(
                {
                    "code": "xmax.contract_error",
                    "message": f"benchmark status {contract['status']!r} is not "
                    "executable (shadow/active required)",
                    "stage": "evaluate",
                    "retryable": False,
                }
            )
        active_without_judge = []
        for dimension in contract["dimensions"]:
            routing = dimension.get("judge_routing", {})
            primary = routing.get("primary_kinds", [])
            if dimension.get("status") == "active" and not primary:
                active_without_judge.append(dimension["dimension_id"])
        if active_without_judge:
            self.report.add_item(
                {
                    "code": "xmax.missing_dependency",
                    "message": "active dimensions without primary judge: "
                    + ", ".join(active_without_judge),
                    "stage": "evaluate",
                    "retryable": False,
                }
            )

    def _check_scenario_pack(self, request: dict[str, Any]) -> None:
        path = self.root / request.get("scenario_pack_path", "config/scenarios.json")
        if not path.is_file():
            self.report.add_item(
                {
                    "code": "xmax.missing_input",
                    "message": f"scenario pack missing: {path}",
                    "stage": "plan",
                    "retryable": False,
                }
            )
            return
        try:
            load_scenario_pack(path)
        except XmaxTestError as exc:
            self.report.add(exc)

    def _check_operation_recipes(self, request: dict[str, Any]) -> None:
        path = self.root / "config" / "operation-recipes.json"
        if not path.is_file():
            self.report.add_item(
                {
                    "code": "xmax.missing_input",
                    "message": f"operation recipe pack missing: {path}",
                    "stage": "plan",
                    "retryable": False,
                }
            )
            return
        try:
            pack = load_config(path, "operation-recipes.schema.json", base_dir=self.root)
        except XmaxTestError as exc:
            self.report.add(exc)
            return
        for recipe in pack.get("recipes", []):
            default = recipe.get("default_generation_mode")
            allowed = recipe.get("allowed_generation_modes", [])
            if default not in allowed:
                self.report.add_item(
                    {
                        "code": "xmax.contract_error",
                        "message": f"recipe {recipe.get('recipe_id')} default mode "
                        f"{default!r} not in allowed modes",
                        "stage": "plan",
                        "retryable": False,
                    }
                )
            for mode in allowed:
                if mode not in recipe.get("bindings", {}):
                    self.report.add_item(
                        {
                            "code": "xmax.contract_error",
                            "message": f"recipe {recipe.get('recipe_id')} lacks "
                            f"bindings for mode {mode!r}",
                            "stage": "plan",
                            "retryable": False,
                        }
                    )

    def _check_judges(self, request: dict[str, Any]) -> None:
        path = self.root / "config" / "judges.json"
        if not path.is_file():
            self.report.add_item(
                {
                    "code": "xmax.missing_input",
                    "message": "judges registry missing: config/judges.json",
                    "stage": "evaluate",
                    "retryable": False,
                }
            )
            return
        try:
            pack = load_config(path, "judge-registry.schema.json", base_dir=self.root)
        except XmaxTestError as exc:
            self.report.add(exc)
            return
        enabled = [item for item in pack.get("judges", []) if item.get("enabled")]
        if not enabled and "evaluate" in request.get("stages", []):
            self.report.add_item(
                {
                    "code": "xmax.missing_dependency",
                    "message": "no enabled judge in config/judges.json",
                    "stage": "evaluate",
                    "retryable": False,
                }
            )
        for judge in enabled:
            kind = judge.get("kind")
            if kind in {"mlmm", "mlmm_cli"}:
                provider = judge.get("provider", {})
                provider_type = provider.get("type", "codex_cli")
                if provider_type == "codex_cli":
                    binary = provider.get("binary") or judge.get("entrypoint", "codex")
                else:
                    binary = None
                if binary and not _command_exists(binary):
                    self.report.add_item(
                        {
                            "code": "xmax.missing_dependency",
                            "message": f"MLLM judge {judge.get('judge_id')} binary "
                            f"not found on PATH: {binary}",
                            "stage": "evaluate",
                            "retryable": False,
                        }
                    )
                if provider_type == "openai_compatible":
                    import os

                    api_key_env = provider.get("api_key_env", "")
                    credential_csv = provider.get("credential_csv")
                    credential_path = None
                    if credential_csv:
                        credential_path = Path(credential_csv)
                        if not credential_path.is_absolute():
                            credential_path = self.root / credential_path
                    has_environment_key = bool(
                        api_key_env and os.getenv(api_key_env)
                    )
                    has_credential_csv = bool(
                        credential_path and credential_path.is_file()
                    )
                    if not has_environment_key and not has_credential_csv:
                        self.report.add_item(
                            {
                                "code": "xmax.missing_dependency",
                                "message": (
                                    f"MLLM judge {judge.get('judge_id')} needs either "
                                    f"API key env {api_key_env!r} or credential CSV "
                                    f"{str(credential_path)!r}"
                                ),
                                "stage": "evaluate",
                                "retryable": False,
                            }
                        )

        if "evaluate" in request.get("stages", []):
            benchmark_path = self.root / request.get(
                "benchmark_path", "BENCHMARK.md"
            )
            if benchmark_path.is_file():
                try:
                    contract = load_benchmark_contract(benchmark_path)
                except (XmaxTestError, ValueError):
                    contract = None
                if contract is not None:
                    modes = request.get("generation_modes") or [
                        "offline",
                        "realtime",
                    ]
                    self.judge_coverage = _judge_coverage(
                        contract, enabled, modes
                    )
                    for mode, missing in self.judge_coverage["missing_by_mode"].items():
                        if missing:
                            self.report.add_item(
                                {
                                    "code": "xmax.missing_dependency",
                                    "message": (
                                        f"{mode} benchmark dimensions without a compatible "
                                        f"enabled judge: {', '.join(missing)}"
                                    ),
                                    "stage": "evaluate",
                                    "retryable": False,
                                }
                            )

    def _check_feishu(self, stages: list[str]) -> None:
        if not {"sync", "reconcile", "ingest"} & set(stages):
            return
        path = self.root / "config" / "feishu.json"
        if not path.is_file():
            self.report.add_item(
                {
                    "code": "xmax.missing_input",
                    "message": "sync/reconcile/ingest needs config/feishu.json "
                    "(copy config/feishu.example.json)",
                    "stage": "sync",
                    "retryable": False,
                }
            )
            return
        try:
            config = load_json(path)
        except XmaxTestError as exc:
            self.report.add(exc)
            return
        required_tables = ("feed_data", "prompt_data", "case_data")
        missing_tables = [
            table for table in required_tables if not config.get("tables", {}).get(table)
        ]
        if missing_tables:
            self.report.add_item(
                {
                    "code": "xmax.missing_input",
                    "message": "feishu config missing tables: " + ", ".join(missing_tables),
                    "stage": "sync",
                    "retryable": False,
                }
            )

    def _check_billed_approval(self, stages: list[str], request: dict[str, Any]) -> None:
        if not (set(stages) & BILLED_STAGES):
            return
        if request.get("dry_run"):
            return
        if not request.get("budget_approved"):
            self.report.add_item(
                {
                    "code": "xmax.approval_required",
                    "message": "generate stage requires an approved budget preview "
                    "before the real run",
                    "stage": "generate",
                    "retryable": False,
                }
            )

    def _check_report_inputs(self, stages: list[str], request: dict[str, Any]) -> None:
        if "report" not in stages:
            return
        comparison = request.get("comparison", {})
        for key in (
            "baseline_model_version",
            "candidate_model_version",
            "requested_scene_ids",
            "report_template_path",
        ):
            if not comparison.get(key):
                self.report.add_item(
                    {
                        "code": "xmax.missing_input",
                        "message": f"report comparison missing {key}",
                        "stage": "report",
                        "retryable": False,
                    }
                )
        template = self.root / comparison.get("report_template_path", "")
        if template.is_file() is False:
            self.report.add_item(
                {
                    "code": "xmax.missing_input",
                    "message": f"report template missing: {template}",
                    "stage": "report",
                    "retryable": False,
                }
            )

    def _check_existing_results(self, request: dict[str, Any]) -> None:
        if "ingest" not in request.get("stages", []):
            return
        path = request.get("existing_results_request_path")
        if not path:
            return
        path = self.root / path
        if not path.is_file():
            self.report.add_item(
                {
                    "code": "xmax.missing_input",
                    "message": f"existing-results request missing: {path}",
                    "stage": "ingest",
                    "retryable": False,
                }
            )
            return
        try:
            config = load_json(path)
        except XmaxTestError as exc:
            self.report.add(exc)
            return
        if config.get("write_remote"):
            self.report.add_item(
                {
                    "code": "xmax.contract_error",
                    "message": "ingest results must keep write_remote=false",
                    "stage": "ingest",
                    "retryable": False,
                }
            )
        mapping = config.get("field_mapping", {})
        for key in ("case_number", "result_attachment", "model_version"):
            if not mapping.get(key):
                self.report.add_item(
                    {
                        "code": "xmax.missing_input",
                        "message": f"existing-results field_mapping missing {key}",
                        "stage": "ingest",
                        "retryable": False,
                    }
                )

    def _check_keys(self, request: dict[str, Any], stages: list[str]) -> None:
        dotenv = self.root / ".env"
        env = load_dotenv(dotenv) if dotenv.is_file() else {}
        if "generate" in stages and not request.get("dry_run"):
            project_path = self.root / "config" / "project.json"
            project = load_json(project_path) if project_path.is_file() else {}
            key_path = project.get("xmax_api_key_file")
            if key_path and not Path(key_path).is_absolute():
                key_path = self.root / key_path
            if not (
                env.get("XMAX_API_KEY")
                or _env("XMAX_API_KEY")
                or secret_file(key_path)
            ):
                self.report.add_item(
                    {
                        "code": "xmax.missing_dependency",
                        "message": "XMAX_API_KEY missing for real generation",
                        "stage": "generate",
                        "retryable": False,
                    }
                )

    def _check_local_dependencies(self, request: dict[str, Any], stages: list[str]) -> None:
        def missing(binary: str, stage: str, purpose: str) -> None:
            if not _command_exists(binary):
                self.report.add_item(
                    {
                        "code": "xmax.missing_dependency",
                        "message": f"{binary} missing; required for {purpose}",
                        "stage": stage,
                        "retryable": False,
                    }
                )

        if "preprocess" in stages:
            missing("ffmpeg", "preprocess", "real frame extraction")
            missing("ffprobe", "preprocess", "media validation")

        modes = set(request.get("generation_modes", []))
        if "generate" in stages and not request.get("dry_run"):
            if not modes or "offline" in modes:
                try:
                    from qcloud_cos import CosConfig, CosS3Client  # noqa: F401
                except ImportError:
                    self.report.add_item(
                        {
                            "code": "xmax.missing_dependency",
                            "message": "cos-python-sdk-v5 missing for real offline asset upload; install .[production]",
                            "stage": "generate",
                            "retryable": False,
                        }
                    )
            if "realtime" in modes:
                missing("node", "generate", "realtime browser harness")
                missing("npm", "generate", "realtime browser harness")
                package = self.root / "realtime-harness" / "node_modules" / "@xmaxai" / "sdk"
                playwright = self.root / "realtime-harness" / "node_modules" / "playwright"
                for path, label in ((package, "@xmaxai/sdk"), (playwright, "playwright")):
                    if not path.exists():
                        self.report.add_item(
                            {
                                "code": "xmax.missing_dependency",
                                "message": f"{label} missing in realtime-harness; run npm install",
                                "stage": "generate",
                                "retryable": False,
                            }
                        )

        if {"sync", "reconcile", "ingest"} & set(stages):
            path = self.root / "config" / "feishu.json"
            if path.is_file():
                try:
                    config = load_json(path)
                except XmaxTestError:
                    return
                if config.get("connection", {}).get("provider") == "lark-cli":
                    missing("lark-cli", "sync", "Feishu read/write adapter")

    def summary(self) -> dict[str, Any]:
        summary = {
            "ok": self.report.is_empty(),
            "error_count": len(self.report.errors),
            "errors": redact(self.report.errors),
        }
        if self.judge_coverage:
            summary["judge_coverage"] = self.judge_coverage
        return summary


def _env(name: str) -> str | None:
    import os

    return os.environ.get(name)


def _command_exists(binary: str) -> bool:
    from shutil import which

    return which(binary) is not None or Path(binary).is_file()


def check_run_request(root: Path, request: dict[str, Any]) -> dict[str, Any]:
    return ContextChecker(root).check_run_request(request)


def _judge_coverage(
    benchmark: dict[str, Any],
    enabled_judges: list[dict[str, Any]],
    requested_modes: list[str],
) -> dict[str, Any]:
    """Return declared Judge coverage without executing external providers."""

    modes = [mode for mode in ("offline", "realtime") if mode in requested_modes]
    missing_by_mode: dict[str, list[str]] = {mode: [] for mode in modes}
    covered_by_mode: dict[str, list[str]] = {mode: [] for mode in modes}
    for mode in modes:
        for dimension in benchmark.get("dimensions", []):
            applicable = set(dimension.get("applicable_modes", []))
            if mode not in applicable and "both" not in applicable:
                continue
            routing = dimension.get("judge_routing", {})
            allowed_kinds = set(routing.get("primary_kinds", [])) | set(
                routing.get("secondary_kinds", [])
            )
            dimension_id = dimension.get("dimension_id", "")
            compatible = False
            for judge in enabled_judges:
                kind = judge.get("kind")
                normalized_kind = "mlmm" if kind == "mlmm_cli" else kind
                if allowed_kinds and normalized_kind not in allowed_kinds:
                    continue
                if dimension_id not in judge.get("supported_dimensions", []):
                    continue
                if mode not in judge.get("supported_modes", []):
                    continue
                compatible = True
                break
            target = covered_by_mode if compatible else missing_by_mode
            target[mode].append(dimension_id)
    return {
        "complete": not any(missing_by_mode.values()),
        "covered_by_mode": covered_by_mode,
        "missing_by_mode": missing_by_mode,
        "covered_counts": {
            mode: len(items) for mode, items in covered_by_mode.items()
        },
    }
