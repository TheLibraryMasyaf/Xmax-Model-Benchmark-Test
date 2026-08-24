"""Composition root and unified CLI.

Wires real adapters from configuration; every external boundary keeps a
deterministic fake so the whole pipeline runs offline. Output supports human
readable text and ``--json``. Exit codes follow ``docs/implementation-contract.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .benchmark import load_benchmark_contract
from .config import env_secret, load_config, redact, secret_file
from .context import ContextChecker
from .errors import (
    EXIT_APPROVAL_REQUIRED,
    EXIT_EXTERNAL_FAILURE,
    EXIT_INPUT_ERROR,
    EXIT_INTERNAL,
    EXIT_MISSING_DEPENDENCY,
    EXIT_PARTIAL,
    ConfigError,
    XmaxTestError,
)
from .scenarios import load_scenario_pack
from .storage.artifacts import ArtifactStore
from .storage.migrations import migrate
from .storage.sqlite import SqliteMetadataRepository
from .time import SystemClock

REQUIRED_PROJECT_FILES = (
    "AGENTS.md",
    "IMPLEMENTATION.md",
    "README.md",
    "report-templates/README.md",
    "report-templates/model-version-update-report.md",
    "report-templates/single-version-evaluation-report.md",
    "RUNBOOK.md",
    "BENCHMARK.md",
    "config/asset-sources.example.json",
    "config/existing-results.example.json",
    "config/feishu.example.json",
    "config/human-feedback-source.example.json",
    "config/interaction-profiles.example.json",
    "config/interaction-profiles.json",
    "config/judges.example.json",
    "config/operation-recipes.example.json",
    "config/operation-recipes.json",
    "config/project.example.json",
    "config/run-evaluate-only.example.json",
    "config/run-generate-only.example.json",
    "config/run-import-evaluate-only.example.json",
    "config/run-request.example.json",
    "config/run-task-allocation.example.json",
    "config/run-sync-scores-only.example.json",
    "config/run-smoke-5x5.example.json",
    "config/single-version-report.example.json",
    "config/scenarios.json",
    "docs/architecture.md",
    "docs/data-contracts.md",
    "docs/decisions.md",
    "docs/external-inputs.md",
    "docs/fail-safe-checks.md",
    "docs/feishu-database.md",
    "docs/implementation-contract.md",
    "docs/stage-orchestration.md",
    "docs/task-execution.md",
    "docs/operation-recipes.md",
    "docs/scene-weighting.md",
    "docs/storage.md",
    "docs/version-reporting.md",
    "docs/single-version-reporting.md",
    "schemas/benchmark.schema.json",
    "schemas/batch-manifest.schema.json",
    "schemas/asset-sources.schema.json",
    "schemas/evaluation-result.schema.json",
    "schemas/existing-results.schema.json",
    "schemas/feishu-config.schema.json",
    "schemas/human-feedback-source.schema.json",
    "schemas/judge-registry.schema.json",
    "schemas/interaction-profiles.schema.json",
    "schemas/model-version-report.schema.json",
    "schemas/single-version-report-request.schema.json",
    "schemas/single-version-report.schema.json",
    "schemas/operation-recipes.schema.json",
    "schemas/project-config.schema.json",
    "schemas/pipeline-selector.schema.json",
    "schemas/run-request.schema.json",
    "schemas/scenario-pack.schema.json",
    "schemas/stage-manifest.schema.json",
    "schemas/test-plan.schema.json",
    "schemas/test-task.schema.json",
)


class Composition:
    """Wires repositories, stores, contracts and fakes from a project root."""

    PACKAGE_ROOT = Path(__file__).resolve().parents[2]

    def __init__(
        self,
        root: Path,
        *,
        project_config: dict[str, Any] | None = None,
        inject: dict[str, Any] | None = None,
    ) -> None:
        from .media_runtime import ensure_media_tools

        ensure_media_tools()
        self.root = Path(root)
        self.clock = SystemClock()
        self.inject = inject or {}

        if project_config is not None:
            self.project = project_config
        else:
            project_path = self.root / "config" / "project.json"
            if project_path.is_file():
                self.project = load_config(
                    project_path, "project-config.schema.json", base_dir=self.root
                )
            else:
                example = load_config(
                    self.PACKAGE_ROOT / "config" / "project.example.json",
                    "project-config.schema.json",
                    base_dir=self.PACKAGE_ROOT,
                )
                # Example paths are relative to the package (project) root.
                self.project = {
                    key: value for key, value in example.items() if not key.startswith("_")
                }

        self.database = SqliteMetadataRepository(
            self.project.get("database_path", str(self.root / "var" / "xmax-test.sqlite3")),
            clock=self.clock,
        )
        self.artifacts = ArtifactStore(
            self.project.get("artifact_root", str(self.root / "var" / "artifacts"))
        )
        self.manifest_root = Path(
            self.project.get("manifest_root", str(self.root / "var" / "manifests"))
        )
        self.manifest_root.mkdir(parents=True, exist_ok=True)

        benchmark_path = self._path("benchmark_path", "BENCHMARK.md")
        self.benchmark = load_benchmark_contract(benchmark_path)
        scenario_path = self._path("scenario_pack_path", "config/scenarios.json")
        self.scenario_pack = load_scenario_pack(scenario_path)
        self.recipes = self._build_recipes()
        self.interactions = self._build_interactions()
        self._evaluation_budget_gate = None
        self.judges = self._build_judges()

    # ------------------------------------------------------------------
    def _path(self, key: str, default: str) -> Path:
        value = self.project.get(key, default)
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate

    def _build_recipes(self) -> Any:
        from .planning.recipes import RecipeResolver

        path = self._path("operation_recipe_path", "config/operation-recipes.json")
        return RecipeResolver(path)

    def xmax_api_key(self) -> str | None:
        return env_secret("XMAX_API_KEY", self.root / ".env") or secret_file(
            self.project.get("xmax_api_key_file")
        )

    def _build_interactions(self) -> Any:
        from .generation.realtime.interactions import InteractionProfileResolver

        path = self.root / "config" / "interaction-profiles.json"
        if not path.is_file():
            path = self.PACKAGE_ROOT / "config" / "interaction-profiles.json"
        pack = load_config(path, "interaction-profiles.schema.json")
        return InteractionProfileResolver(pack)

    def realtime_case_config(self, case: dict[str, Any], *, headed: bool = False) -> dict[str, Any]:
        bindings = case.get("api_asset_bindings", {})
        width, height = 1280, 720
        interaction = self.interactions.expand(
            bindings.get("interaction_profile_id"), width=width, height=height
        )
        return {
            "headed": headed,
            "content_width": width,
            "content_height": height,
            "tracks": interaction["tracks"],
            "interaction_profile": interaction["profile"],
        }

    def _build_judges(self) -> Any:
        import importlib

        from .judges.mlmm import MlmmJudge
        from .judges.plugins.video_quality import VideoQualityJudge
        from .judges.registry import JudgeRegistry

        registry = JudgeRegistry()
        judges_path = self.root / "config" / "judges.json"
        enabled = []
        if judges_path.is_file():
            data = load_config(judges_path, "judge-registry.schema.json", base_dir=self.root)
            enabled = [j for j in data.get("judges", []) if j.get("enabled")]
        for judge in enabled:
            entrypoint = judge.get("entrypoint", "")
            kind = judge.get("kind")
            if kind in {"mlmm", "mlmm_cli"}:
                provider = self.mlmm_provider(judge)
                registry.register(
                    MlmmJudge(
                        provider,
                        artifact_store=self.artifacts,
                        log_dir=self.root / "var" / "logs" / "mlmm",
                        judge_id=judge["judge_id"],
                        version=judge["version"],
                        supported_dimensions=judge.get("supported_dimensions", []),
                        supported_criteria=judge.get("supported_criteria", []),
                        supported_modes=judge.get("supported_modes", ["offline", "realtime"]),
                        max_retries=int(judge.get("max_retries", 2)),
                        calibration_path=(
                            self.root / judge["calibration"]["path"]
                            if judge.get("calibration", {}).get("path")
                            and not Path(judge["calibration"]["path"]).is_absolute()
                            else judge.get("calibration", {}).get("path")
                        ),
                        max_examples_per_dimension=int(
                            judge.get("calibration", {}).get("max_examples_per_dimension", 2)
                        ),
                    )
                )
            elif entrypoint == "xmax_test.judges.plugins.video_quality:VideoQualityJudge":
                registry.register(VideoQualityJudge())
            elif kind in {"cv", "python_plugin", "metric"} and ":" in entrypoint:
                module_name, object_name = entrypoint.split(":", 1)
                plugin_type = getattr(importlib.import_module(module_name), object_name)
                registry.register(plugin_type())
        # Injectable metric judges for e2e tests.
        for plugin in self.inject.get("judges", []):
            registry.register(plugin)
        return registry

    def mlmm_provider(self, judge: dict[str, Any] | None = None) -> Any:
        """Build the configured MLLM transport without binding callers to Codex."""

        import importlib

        from .judges.mlmm import CodexCliProvider, OpenAiCompatibleProvider

        if judge is None:
            path = self.root / "config" / "judges.json"
            data = load_config(path, "judge-registry.schema.json", base_dir=self.root)
            judge = next(
                (
                    item
                    for item in data.get("judges", [])
                    if item.get("enabled") and item.get("kind") in {"mlmm", "mlmm_cli"}
                ),
                None,
            )
            if judge is None:
                raise ConfigError("no enabled MLLM provider in config/judges.json")
        entrypoint = judge.get("entrypoint", "")
        provider_config = dict(judge.get("provider") or {})
        provider_name = provider_config.get("type")
        if not provider_name:
            provider_name = "codex_cli"
            provider_config = {
                **self.project.get("codex", {}),
                **provider_config,
                "binary": entrypoint or self.project.get("codex", {}).get("binary", "codex"),
            }
        if provider_name == "codex_cli":
            return CodexCliProvider(
                binary=provider_config.get("binary", "codex"),
                timeout_s=int(
                    provider_config.get("timeout_seconds", judge.get("timeout_seconds", 300))
                ),
                sandbox=provider_config.get("sandbox", "read-only"),
                model=provider_config.get("model"),
            )
        if provider_name == "openai_compatible":
            from .evaluation.budget import EvaluationBudgetGate, PaidFallbackPolicy

            credential_csv = provider_config.get("credential_csv")
            if credential_csv and not Path(credential_csv).is_absolute():
                credential_csv = self.root / credential_csv
            policy = PaidFallbackPolicy.from_config(provider_config.get("paid_fallback"))
            budget_gate = (
                EvaluationBudgetGate(self.database, policy) if policy is not None else None
            )
            if budget_gate is not None:
                self._evaluation_budget_gate = budget_gate
            return OpenAiCompatibleProvider(
                endpoint=provider_config.get("endpoint"),
                model=provider_config.get("model"),
                models=provider_config.get("models"),
                api_key_env=provider_config.get("api_key_env"),
                credential_csv=credential_csv,
                api_key_csv_field=provider_config.get("api_key_csv_field", "apiKey"),
                endpoint_csv_field=provider_config.get("endpoint_csv_field", "openAiCompatible"),
                timeout_s=int(
                    provider_config.get("timeout_seconds", judge.get("timeout_seconds", 300))
                ),
                extra_headers=provider_config.get("extra_headers", {}),
                response_format_type=provider_config.get("response_format_type", "json_schema"),
                request_options=provider_config.get("request_options", {}),
                direct_media=provider_config.get("direct_media", False),
                video_options=provider_config.get("video_options", {}),
                image_options=provider_config.get("image_options", {}),
                max_base64_bytes=int(provider_config.get("max_base64_bytes", 10_000_000)),
                budget_gate=budget_gate,
                transport_max_retries=int(provider_config.get("transport_max_retries", 2)),
                retry_backoff_seconds=float(
                    provider_config.get("retry_backoff_seconds", 1.0)
                ),
                retry_backoff_max_seconds=float(
                    provider_config.get("retry_backoff_max_seconds", 8.0)
                ),
                retry_jitter_seconds=float(provider_config.get("retry_jitter_seconds", 0.25)),
            )
        if provider_name == "python_plugin":
            provider_entrypoint = provider_config.get("entrypoint", "")
            if ":" not in provider_entrypoint:
                raise ConfigError("python_plugin MLLM provider requires module:object entrypoint")
            module_name, object_name = provider_entrypoint.split(":", 1)
            provider_class = getattr(importlib.import_module(module_name), object_name)
            return provider_class(**provider_config.get("kwargs", {}))
        raise ConfigError(f"unsupported MLLM provider type: {provider_name}")

    def feishu_client(self, *, read_only: bool = False) -> Any:
        if "feishu_sync" in self.inject:
            return self.inject["feishu_sync"]
        if "feishu_read" in self.inject and read_only:
            return self.inject["feishu_read"]
        from .feishu.client import LarkCliSyncClient

        return LarkCliSyncClient()

    def feishu_config(self) -> dict[str, Any]:
        path = self.root / "config" / "feishu.json"
        if path.is_file():
            return load_config(path, "feishu-config.schema.json")
        return load_config(
            self.root / "config" / "feishu.example.json", "feishu-config.schema.json"
        )

    # ------------------------------------------------------------------
    def offline_adapter(self, *, run_batch_id: str, model_id: str) -> Any:
        from .generation.fakes import FakeResultSource, build_offline_adapter
        from .generation.offline.adapter import OfflineGenerationAdapter
        from .generation.offline.repository import OfflineRunRepository
        from .generation.offline.rest_adapter import HttpOfflineTaskTransport

        run_repo = OfflineRunRepository(self.database, self.artifacts)
        transport = self.inject.get("offline_transport")
        session_api = self.inject.get("session_api")
        rtc = self.inject.get("rtc")
        if transport is not None or session_api is not None or rtc is not None:
            return build_offline_adapter(
                run_repo,
                self.artifacts,
                self._media_validator(),
                run_repo,
                transport=transport,
                session_api=session_api,
                rtc=rtc,
                model_id=model_id,
                run_batch_id=run_batch_id,
                clock=self.clock,
            )
        api_key = self.xmax_api_key()
        if not api_key:
            from .errors import MissingDependencyError

            raise MissingDependencyError("XMAX_API_KEY is required for real offline generation")
        transport = HttpOfflineTaskTransport(
            base_url=self.project.get("xmax_api_base_url", "https://api.xmaxai.com/open/api/v1"),
            api_key=api_key,
            quality=self.project.get("xmax_offline_quality", "hd"),
            fps=self.project.get("xmax_offline_fps"),
        )
        return OfflineGenerationAdapter(
            run_repo,
            self.artifacts,
            self._media_validator(),
            run_repo,
            backend="rest",
            transport=transport,
            result_source=FakeResultSource(transport),
            model_id=model_id,
            run_batch_id=run_batch_id,
            clock=self.clock,
        )

    def realtime_controller(self, *, run_batch_id: str, model_id: str, headed: bool = False) -> Any:
        from .generation.realtime.controller import RealtimeController

        harness = self.inject.get("realtime_harness")
        if harness is None and not self.inject:
            from .generation.realtime.harness import BrowserRealtimeHarness

            harness = BrowserRealtimeHarness(
                self.root,
                self.artifacts,
                self.database,
                api_key=self.xmax_api_key(),
                headed=headed,
            )

        return RealtimeController(
            self.database,
            self.artifacts,
            harness=harness,
            clock=self.clock,
            model_id=model_id,
            run_batch_id=run_batch_id,
            validator=self._media_validator() if harness is not None else None,
        )

    def preprocess_service(self, repository: Any | None = None) -> Any:
        from .evaluation.frames import FfmpegFrameExtractor
        from .evaluation.preprocess import PreprocessService

        extractor = self.inject.get("frame_extractor")
        if extractor is None and not self.inject:
            extractor = FfmpegFrameExtractor(self.artifacts)
        return PreprocessService(
            repository or self.database,
            self.artifacts,
            frame_extractor=extractor,
            clock=self.clock,
        )

    def evaluation_worker(self) -> Any:
        from .judges.worker import JudgeWorker

        return JudgeWorker(self.judges, self.artifacts)

    def evaluation_orchestrator(self, repository: Any | None = None) -> Any:
        from .evaluation.fusion import JudgmentFusion
        from .evaluation.orchestrator import EvaluationOrchestrator

        repository = repository or self.database
        return EvaluationOrchestrator(
            repository,
            self.artifacts,
            self.benchmark,
            self.scenario_pack,
            self.judges,
            self.evaluation_worker(),
            self.preprocess_service(repository),
            fusion=JudgmentFusion(),
            recipe_resolver=self.recipes,
            budget_gate=self._evaluation_budget_gate,
            clock=self.clock,
        )

    def evaluation_budget_gate(self) -> Any:
        if self._evaluation_budget_gate is None:
            raise ConfigError("no enabled paid_fallback is configured for MLLM evaluation")
        return self._evaluation_budget_gate

    def plan_builder(self) -> Any:
        from .planning.builder import TestPlanBuilder
        from .planning.case_numbers import CaseNumberAllocator

        return TestPlanBuilder(
            self.database,
            self.recipes,
            self.scenario_pack,
            self.benchmark,
            allocator=CaseNumberAllocator(self.database),
            clock=self.clock,
        )

    def asset_source_factory(self, download_dir: Path) -> Callable[[dict], Any]:
        from .assets.sources import build_source

        def factory(item: dict) -> Any:
            return build_source(
                item, client_factory=lambda kind: self.feishu_client(read_only=True)
            )

        return factory

    def ingest_service(self, download_dir: Path | None = None) -> Any:
        from .assets.registry import AssetRegistry, SourceRunner
        from .ingest.results import ImportService

        registry = AssetRegistry(self.database, self.artifacts, self._media_validator())
        download_dir = download_dir or self.root / "var" / "downloads"
        runner = SourceRunner(
            registry,
            self.asset_source_factory(download_dir),
            download_dir,
            clock=self.clock,
        )
        importer = ImportService(
            self.database, registry, self.recipes, download_dir, clock=self.clock
        )
        return {"registry": registry, "runner": runner, "importer": importer}

    def _media_validator(self) -> Any:
        from .assets.validator import MediaValidator

        probe = self.inject.get("probe")
        if probe is not None:
            return MediaValidator(probe=probe)
        return MediaValidator()

    def feishu_sync_service(self) -> Any:
        from .feishu.attachments import AttachmentUploader
        from .feishu.ledger import SyncLedger
        from .feishu.sync import FeishuSyncService

        client = self.feishu_client()
        ledger = SyncLedger(self.database)
        uploader = AttachmentUploader(client)
        config = self.feishu_config()
        return FeishuSyncService(
            client,
            ledger,
            uploader,
            config,
            self.database,
            self.artifacts,
            clock=self.clock,
            benchmark=self.benchmark,
        )

    def feishu_reconcile(self) -> Any:
        from .feishu.reconcile import ReconcileService

        return ReconcileService(self.feishu_client(), self.feishu_config(), self.database)

    def reporting_service(self) -> Any:
        from .reporting.comparison import ModelComparisonService
        from .reporting.repository import ReportRepository
        from .reporting.service import ModelUpdateReportService

        schema = _score_schema_of(self.benchmark)
        return ModelUpdateReportService(
            ReportRepository(self.database),
            ModelComparisonService(self.database, self.benchmark, self.scenario_pack, schema),
            self.benchmark,
            self.scenario_pack,
            schema,
        )

    def single_version_reporting_service(self) -> Any:
        from .reporting.single_version import SingleVersionReportService

        return SingleVersionReportService(self.database, self.benchmark, self.scenario_pack)

    def score_schema(self) -> dict[str, Any]:
        return _score_schema_of(self.benchmark)

    def close(self) -> None:
        self.database.close()


def _score_schema_of(benchmark: dict[str, Any]) -> dict[str, Any]:
    for schema in benchmark.get("score_schemas", []):
        if schema.get("status") in {"active", "shadow", "draft"}:
            return schema
    return {}


# ----------------------------------------------------------------------
# command implementations
# ----------------------------------------------------------------------
def cmd_project_check(composition: Composition, args: argparse.Namespace) -> int:
    missing = [item for item in REQUIRED_PROJECT_FILES if not (composition.root / item).exists()]
    data = {"ok": not missing, "missing": missing}
    _emit(args, "project.check", data, ok=data["ok"])
    return 0 if data["ok"] else EXIT_INPUT_ERROR


def cmd_benchmark_check(composition: Composition, args: argparse.Namespace) -> int:
    data = {
        "ok": True,
        "benchmark_version": composition.benchmark.get("benchmark_version"),
        "status": composition.benchmark.get("status"),
        "dimensions": len(composition.benchmark.get("dimensions", [])),
        "weight_profiles": len(composition.benchmark.get("weight_profiles", [])),
        "scene_weight_rules": len(composition.benchmark.get("scene_weight_rules", [])),
        "score_schemas": len(composition.benchmark.get("score_schemas", [])),
        "scenarios": len(composition.scenario_pack.get("scenarios", [])),
    }
    _emit(args, "benchmark.check", data)
    return 0


def cmd_db_migrate(composition: Composition, args: argparse.Namespace) -> int:
    applied = migrate(composition.database._conn)
    _emit(args, "db.migrate", {"applied": applied})
    return 0


def cmd_db_check(composition: Composition, args: argparse.Namespace) -> int:
    from .storage.migrations import applied_migrations

    versions = sorted(applied_migrations(composition.database._conn))
    current_benchmark = str(composition.benchmark.get("benchmark_version", ""))
    evaluation_versions: dict[str, int] = {}
    legacy_evaluation_ids: list[str] = []
    for result in composition.database.list_evaluation_results():
        version = str(result.get("benchmark_version") or "unknown")
        evaluation_versions[version] = evaluation_versions.get(version, 0) + 1
        if version != current_benchmark:
            legacy_evaluation_ids.append(str(result.get("evaluation_id", "")))
    _emit(
        args,
        "db.check",
        {
            "migrations": versions,
            "ok": bool(versions),
            "current_benchmark_version": current_benchmark,
            "evaluation_versions": evaluation_versions,
            "legacy_evaluation_count": len(legacy_evaluation_ids),
            "legacy_evaluation_ids": sorted(legacy_evaluation_ids),
            "legacy_action": (
                "keep immutable; replay an exact Run Batch under the current Benchmark"
                if legacy_evaluation_ids
                else None
            ),
        },
    )
    return 0 if versions else EXIT_INPUT_ERROR


def cmd_artifacts_verify(composition: Composition, args: argparse.Namespace) -> int:
    results = []
    for uri in sorted(composition.artifacts.list()):
        try:
            results.append(composition.artifacts.verify(uri))
        except Exception as exc:
            results.append({"uri": uri, "ok": False, "error": str(exc)})
    _emit(args, "artifacts.verify", {"results": results})
    return 0


def cmd_artifacts_gc(composition: Composition, args: argparse.Namespace) -> int:
    entries = composition.artifacts.gc(dry_run=args.dry_run)
    _emit(
        args,
        "artifacts.gc",
        {"deleted": sum(1 for e in entries if e["deleted"]), "entries": entries},
    )
    return 0


def cmd_context_check(composition: Composition, args: argparse.Namespace) -> int:
    request = load_config(Path(args.request), "run-request.schema.json", base_dir=composition.root)
    checker = ContextChecker(composition.root)
    checker.check_run_request(request)
    summary = checker.summary()
    _emit(args, "context.check", summary, ok=summary["ok"])
    return 0 if summary["ok"] else EXIT_INPUT_ERROR


def cmd_run(composition: Composition, args: argparse.Namespace) -> int:
    request = load_config(Path(args.request), "run-request.schema.json", base_dir=composition.root)
    checked_request = {
        **request,
        "dry_run": bool(args.dry_run or request.get("dry_run")),
        "budget_approved": bool(args.budget_approved),
    }
    checker = ContextChecker(composition.root)
    checker.check_run_request(checked_request)
    preflight = checker.summary()
    if not preflight["ok"]:
        _emit(args, "run.preflight", preflight, ok=False)
        codes = {item.get("code") for item in preflight.get("errors", [])}
        if "xmax.approval_required" in codes:
            return EXIT_APPROVAL_REQUIRED
        if "xmax.missing_dependency" in codes:
            return EXIT_MISSING_DEPENDENCY
        if "xmax.external_failure" in codes:
            return EXIT_EXTERNAL_FAILURE
        return EXIT_INPUT_ERROR
    request = {
        **request,
        "_contract_fingerprints": _contract_fingerprints(composition, request),
    }
    return _execute_run_request(composition, request, args)


def _contract_fingerprints(composition: Composition, request: dict[str, Any]) -> dict[str, str]:
    """Bind stage reuse to the exact benchmark, judges and Feishu projection."""

    from .hashing import content_hash

    candidates = {
        "benchmark": Path(request.get("benchmark_path", composition.root / "BENCHMARK.md")),
        "scenario_pack": Path(
            request.get("scenario_pack_path", composition.root / "config/scenarios.json")
        ),
        "judges": composition.root / "config/judges.json",
        "feishu": composition.root / "config/feishu.json",
    }
    result: dict[str, str] = {}
    for name, path in candidates.items():
        if path.is_file():
            result[name] = content_hash(
                {
                    "path": str(path.resolve()),
                    "content": path.read_text(encoding="utf-8"),
                }
            )
    return result


def _execute_run_request(
    composition: Composition, request: dict[str, Any], args: argparse.Namespace
) -> int:
    from .contracts import PipelineStage
    from .pipeline.manifests import ManifestStore
    from .pipeline.orchestrator import PipelineOrchestrator
    from .pipeline.selectors import SelectorResolver

    manifest_store = ManifestStore(composition.manifest_root, composition.database)
    selectors = SelectorResolver(composition.database, composition.manifest_root)
    executors: dict[PipelineStage, Any] = {}
    stages = request.get("stages", [])
    stream_state: dict[str, Any] = {}

    if "ingest" in stages:
        executors[PipelineStage.INGEST] = _ingest_executor(composition, request, args)
    if "plan" in stages:
        executors[PipelineStage.PLAN] = _plan_executor(composition, request, args)
    if "generate" in stages:
        executors[PipelineStage.GENERATE] = _generate_executor(
            composition, request, args, stream_state
        )
    if "preprocess" in stages:
        executors[PipelineStage.PREPROCESS] = _preprocess_executor(
            composition, request, stream_state
        )
    if "evaluate" in stages:
        executors[PipelineStage.EVALUATE] = _evaluate_executor(composition, request, stream_state)
    if "report" in stages:
        executors[PipelineStage.REPORT] = _report_executor(composition, request, args)
    if "sync" in stages:
        executors[PipelineStage.SYNC] = _sync_executor(composition, request, args)
    if "reconcile" in stages:
        executors[PipelineStage.RECONCILE] = _reconcile_executor(composition, request)

    orchestrator = PipelineOrchestrator(
        composition.database,
        manifest_store,
        selectors,
        executors,
        clock=composition.clock,
    )
    try:
        summary = orchestrator.run(
            request,
            dry_run=args.dry_run,
            resume=args.resume,
            smoke_limit=args.smoke_limit,
            budget_approved=args.budget_approved,
        )
    except XmaxTestError as exc:
        _emit(args, "run", {"errors": [exc.to_dict()]}, ok=False)
        return exc.exit_code
    _emit(args, "run", summary)
    return 0


def _ingest_executor(composition: Composition, request: dict[str, Any], args: argparse.Namespace):
    from .contracts import PipelineStage
    from .pipeline.models import StageExecutionRequest, StageExecutionResult

    def execute(req: StageExecutionRequest) -> StageExecutionResult:
        if req.dry_run:
            return StageExecutionResult(
                status="completed",
                output_refs=[],
                metadata={
                    "dry_run": True,
                    "would_import_existing_results": bool(
                        request.get("existing_results_request_path")
                    ),
                },
            )
        services = composition.ingest_service()
        existing = request.get("existing_results_request_path")
        result = StageExecutionResult(status="completed", output_refs=[], batch_manifests=[])
        if existing:
            config = load_config(composition.root / existing, "existing-results.schema.json")
            outcome = services["importer"].import_results(
                config, feishu_client=composition.feishu_client(read_only=True)
            )
            if outcome.run_batch_id:
                from .pipeline.manifests import build_batch_manifest

                result.batch_manifests.append(
                    build_batch_manifest(
                        entity_type="run_batch",
                        item_entity_type="generation_run",
                        item_ids=[
                            r["run_id"]
                            for r in composition.database.list_runs(
                                run_batch_id=outcome.run_batch_id
                            )
                        ],
                        producer_stage_run_id=req.stage_run_id,
                    )
                )
            result.errors = outcome.errors
        else:
            sources_path = composition.root / "config" / "asset-sources.json"
            if not sources_path.is_file():
                sources_path = composition.root / "config" / "asset-sources.example.json"
            sources = load_config(sources_path, "asset-sources.schema.json")
            summary = services["runner"].sync(sources)
            result.errors = summary["errors"]
            asset_ids = [a["asset_id"] for a in composition.database.list_assets()]
            from .pipeline.manifests import build_batch_manifest

            result.batch_manifests.append(
                build_batch_manifest(
                    entity_type="asset_batch",
                    item_entity_type="asset",
                    item_ids=asset_ids,
                    producer_stage_run_id=req.stage_run_id,
                )
            )
        return result

    return _stage_wrapper(PipelineStage.INGEST, execute)


def _plan_executor(composition: Composition, request: dict[str, Any], args: argparse.Namespace):
    from .contracts import PipelineStage
    from .pipeline.manifests import ref_for_plan
    from .pipeline.models import StageExecutionRequest, StageExecutionResult

    def execute(req: StageExecutionRequest) -> StageExecutionResult:
        builder = composition.plan_builder()
        plan_request = {
            "asset_batch_ids": [
                ref.entity_id for ref in req.input_refs if ref.entity_type == "asset_batch"
            ],
            "model_id": composition.project.get("default_model", "x2.0"),
            "repeat_count": request.get(
                "repeat_count", composition.project.get("default_repeats", 5)
            ),
            "generation_modes": request.get("generation_modes", ["offline"]),
            "generation_mode_overrides": request.get("generation_mode_overrides", []),
            "filters": request.get("filters", {}),
            "seed": request.get("seed"),
            "combination_selection": request.get(
                "combination_selection", {"strategy": "cartesian"}
            ),
            "generation_config": {
                "quality": composition.project.get("xmax_offline_quality", "hd"),
                "fps": composition.project.get("xmax_offline_fps", 24),
                "duration_s": 3,
                **request.get("generation_config", {}),
            },
        }
        if args.dry_run:
            preview = builder.preview(plan_request)
            return StageExecutionResult(
                status="completed", output_refs=[], metadata={"preview": preview}
            )
        plan = builder.build(plan_request)
        from .pipeline.manifests import build_batch_manifest

        task_manifest = build_batch_manifest(
            entity_type="task_batch",
            item_entity_type="test_task",
            item_ids=plan["task_ids"],
            producer_stage_run_id=req.stage_run_id,
            batch_id=plan["task_batch_id"],
            metadata={
                "plan_id": plan["plan_id"],
                "allocation": plan["metadata"]["allocation"],
            },
        )
        return StageExecutionResult(
            status="completed",
            output_refs=[ref_for_plan(plan)],
            batch_manifests=[task_manifest],
        )

    return _stage_wrapper(PipelineStage.PLAN, execute)


def _generate_executor(
    composition: Composition,
    request: dict[str, Any],
    args: argparse.Namespace,
    stream_state: dict[str, Any] | None = None,
):
    from .contracts import PipelineStage
    from .pipeline.manifests import build_batch_manifest
    from .pipeline.models import StageExecutionRequest, StageExecutionResult

    def execute(req: StageExecutionRequest) -> StageExecutionResult:
        if req.dry_run:
            return StageExecutionResult(
                status="completed", output_refs=[], metadata={"dry_run": True}
            )
        plan_ref = next((ref for ref in req.input_refs if ref.entity_type == "test_plan"), None)
        if plan_ref is None:
            return StageExecutionResult(
                status="error",
                output_refs=[],
                errors=[
                    {
                        "code": "xmax.missing_input",
                        "message": "generate requires a test_plan",
                        "stage": "generate",
                        "retryable": False,
                    }
                ],
            )
        plan = composition.database.get_test_plan(plan_ref.entity_id)
        model_id = composition.project.get("default_model", "x2.0")
        batch_id = f"runs-{plan.get('plan_hash', '')[:12]}"
        cases = _smoke_cases(plan.get("cases", []), req.smoke_limit)
        offline_adapter = None
        realtime_controller = None

        # Fail before creating even one GenerationRun when the shared offline
        # upload transport is unavailable or misconfigured. This prevents a
        # batch-wide storm of identical submit_failure rows.
        if any(case.get("generation_mode") == "offline" for case in cases):
            offline_adapter = composition.offline_adapter(run_batch_id=batch_id, model_id=model_id)
            offline_adapter.preflight()

        def generate_case(case: dict[str, Any]) -> dict[str, Any]:
            nonlocal offline_adapter, realtime_controller
            from .planning.builder import generation_signature

            if req.resume:
                candidate_signature = case.get("generation_signature") or generation_signature(case)
                existing = [
                    run
                    for run in composition.database.list_runs(
                        model_id=case.get("model_id") or model_id,
                        status="completed",
                    )
                    if run.get("model_id") == (case.get("model_id") or model_id)
                    and run.get("mode") == case.get("generation_mode", "offline")
                ]
                matching = []
                for run in existing:
                    old_signature = run.get("metrics", {}).get("generation_signature")
                    if not old_signature:
                        try:
                            old_signature = generation_signature(
                                composition.database.get_test_case(run["case_id"])
                            )
                        except Exception:
                            old_signature = None
                    if old_signature == candidate_signature:
                        matching.append(run)
                if matching:
                    reused = max(
                        matching,
                        key=lambda item: (
                            item.get("created_at", ""),
                            item["run_id"],
                        ),
                    )
                    return {**reused, "stream_reused": True}
            if case.get("generation_mode") == "realtime":
                if realtime_controller is None:
                    realtime_controller = composition.realtime_controller(
                        run_batch_id=batch_id, model_id=model_id
                    )
                try:
                    return realtime_controller.run_case(
                        case, composition.realtime_case_config(case)
                    )
                except Exception as exc:
                    # A realtime case that cannot run (e.g. the browser SDK
                    # rejects the input media MIME type) must not trip the
                    # generate circuit breaker and abort the remaining offline
                    # plan.  Signal the streaming coordinator with a dedicated
                    # error so it records the failure and keeps draining.
                    from .errors import RealtimeUnavailableError

                    raise RealtimeUnavailableError(
                        f"realtime case {case['case_id']} cannot run: {exc}",
                        entity_id=case["case_id"],
                    ) from exc
            if offline_adapter is None:
                offline_adapter = composition.offline_adapter(
                    run_batch_id=batch_id, model_id=model_id
                )
            return offline_adapter.run_case(case)

        streaming = request.get(
            "execution_mode", "streaming"
        ) == "streaming" and "preprocess" in request.get("stages", [])
        state = stream_state if stream_state is not None else {}
        errors: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {"execution_mode": "batch"}
        if streaming:
            from .evaluation.preprocess import PROCESSOR_VERSION
            from .hashing import content_hash
            from .pipeline.streaming import StreamingPipelineCoordinator

            evaluation_enabled = "evaluate" in request.get("stages", [])
            preprocess_repository = SqliteMetadataRepository(
                composition.database._path, clock=composition.clock
            )
            evaluation_repository = (
                SqliteMetadataRepository(composition.database._path, clock=composition.clock)
                if evaluation_enabled
                else None
            )
            preprocess_service = composition.preprocess_service(preprocess_repository)
            evaluation_orchestrator = (
                composition.evaluation_orchestrator(evaluation_repository)
                if evaluation_enabled
                else None
            )
            inline_sync_enabled = bool(
                evaluation_enabled
                and "sync" in request.get("stages", [])
                and request.get("sync_policy", "none") != "none"
            )
            sync_service = composition.feishu_sync_service() if inline_sync_enabled else None
            judge_config_path = composition.root / "config" / "judges.json"
            judge_config = (
                json.loads(judge_config_path.read_text(encoding="utf-8"))
                if judge_config_path.is_file()
                else {}
            )
            evaluation_batch_id = (
                "eval-stream-"
                + content_hash(
                    {
                        "plan_id": plan.get("plan_id"),
                        "benchmark_version": composition.benchmark.get("benchmark_version"),
                        "judge_config": judge_config,
                        "preprocessor_version": PROCESSOR_VERSION,
                        "stage_run_id": req.stage_run_id,
                    }
                )[:12]
            )

            def on_generated(run: dict[str, Any]) -> None:
                composition.database.set_run_batch_id(run["run_id"], batch_id)

            def evaluate_one(run: dict[str, Any], preprocess: dict[str, Any]) -> dict[str, Any]:
                existing = evaluation_repository.list_evaluation_results(  # type: ignore[union-attr]
                    run_id=run["run_id"],
                    evaluation_batch_id=evaluation_batch_id,
                )
                if existing:
                    return existing[-1]
                return evaluation_orchestrator.evaluate_run(  # type: ignore[union-attr]
                    run, evaluation_batch_id, preprocess=preprocess
                )

            def sync_one(run: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
                return sync_service.sync_case_run(  # type: ignore[union-attr]
                    run,
                    evaluation,
                    policy=request.get("sync_policy", "full"),
                )

            try:
                coordinator = StreamingPipelineCoordinator(
                    generate_case=generate_case,
                    preprocess_run=preprocess_service.build,
                    evaluate_run=evaluate_one if evaluation_enabled else None,
                    sync_run=sync_one if inline_sync_enabled else None,
                    on_generated=on_generated,
                    queue_size=int(request.get("pipeline_queue_size", 4)),
                    circuit_breaker_threshold=int(request.get("circuit_breaker_threshold", 3)),
                )
                outcome = coordinator.run(cases)
            finally:
                preprocess_repository.close()
                if evaluation_repository is not None:
                    evaluation_repository.close()
            runs = outcome.runs
            errors = outcome.errors["generate"]
            metadata = outcome.metadata
            state.update(
                {
                    "active": True,
                    "run_batch_id": batch_id,
                    "evaluation_batch_id": evaluation_batch_id,
                    "preprocess": outcome.preprocess,
                    "evaluations": outcome.evaluations,
                    "sync": outcome.sync,
                    "errors": outcome.errors,
                    "metadata": outcome.metadata,
                }
            )
        else:
            runs = []
            for case in cases:
                run = generate_case(case)
                composition.database.set_run_batch_id(run["run_id"], batch_id)
                runs.append(run)

        run_ids = [run["run_id"] for run in runs]
        manifest = build_batch_manifest(
            entity_type="run_batch",
            item_entity_type="generation_run",
            item_ids=run_ids,
            producer_stage_run_id=req.stage_run_id,
            batch_id=batch_id,
            metadata=metadata,
        )
        return StageExecutionResult(
            status="partial" if errors else "completed",
            output_refs=[],
            batch_manifests=[manifest],
            errors=errors,
            metadata=metadata,
        )

    return _stage_wrapper(PipelineStage.GENERATE, execute)


def _smoke_cases(cases: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Select a small but mode-representative paid smoke batch."""

    if limit is None or limit >= len(cases):
        return list(cases)
    if limit <= 0:
        return []
    selected: list[dict[str, Any]] = []
    for mode in ("offline", "realtime"):
        match = next((case for case in cases if case.get("generation_mode") == mode), None)
        if match is not None and match not in selected and len(selected) < limit:
            selected.append(match)
    for case in cases:
        if len(selected) >= limit:
            break
        if case not in selected:
            selected.append(case)
    return selected


def _preprocess_executor(
    composition: Composition,
    request: dict[str, Any],
    stream_state: dict[str, Any] | None = None,
):
    from .contracts import PipelineStage
    from .pipeline.manifests import build_batch_manifest
    from .pipeline.models import StageExecutionRequest, StageExecutionResult

    def execute(req: StageExecutionRequest) -> StageExecutionResult:
        if req.dry_run:
            return StageExecutionResult(
                status="completed",
                output_refs=[],
                metadata={"dry_run": True, "preprocess": "validated_only"},
            )
        service = composition.preprocess_service()
        preprocess_ids = []
        run_batch_ref = next(
            (ref for ref in req.input_refs if ref.entity_type == "run_batch"), None
        )
        if run_batch_ref is None:
            return StageExecutionResult(
                status="error",
                output_refs=[],
                errors=[
                    {
                        "code": "xmax.missing_input",
                        "message": "preprocess requires a run_batch",
                        "stage": "preprocess",
                        "retryable": False,
                    }
                ],
            )
        streamed = bool(
            stream_state
            and stream_state.get("active")
            and stream_state.get("run_batch_id") == run_batch_ref.entity_id
        )
        errors: list[dict[str, Any]] = []
        if streamed:
            preprocess_ids = [item["preprocess_id"] for item in stream_state.get("preprocess", [])]
            errors = list(stream_state.get("errors", {}).get("preprocess", []))
        else:
            for run in composition.database.list_runs(run_batch_id=run_batch_ref.entity_id):
                try:
                    result = service.build(run)
                    preprocess_ids.append(result["preprocess_id"])
                except Exception as exc:
                    errors.append(
                        {
                            "code": "xmax.preprocess_error",
                            "message": f"{run.get('run_id')}: {exc}",
                            "stage": "preprocess",
                            "retryable": False,
                            "entity_id": run.get("run_id"),
                        }
                    )
        manifest = build_batch_manifest(
            entity_type="preprocess_batch",
            item_entity_type="preprocess_run",
            item_ids=preprocess_ids,
            producer_stage_run_id=req.stage_run_id,
            metadata={
                "execution_mode": "streaming" if streamed else "batch",
                "streamed_during_generation": streamed,
            },
        )
        return StageExecutionResult(
            status="partial" if errors else "completed",
            output_refs=[],
            batch_manifests=[manifest],
            errors=errors,
            metadata=manifest["metadata"],
        )

    return _stage_wrapper(PipelineStage.PREPROCESS, execute)


def _evaluate_executor(
    composition: Composition,
    request: dict[str, Any],
    stream_state: dict[str, Any] | None = None,
):
    from .contracts import PipelineStage
    from .evaluation.aggregation import aggregate_evaluation_results
    from .pipeline.manifests import build_batch_manifest
    from .pipeline.models import StageExecutionRequest, StageExecutionResult

    def execute(req: StageExecutionRequest) -> StageExecutionResult:
        if req.dry_run:
            return StageExecutionResult(
                status="completed",
                output_refs=[],
                metadata={"dry_run": True, "evaluate": "validated_only"},
            )
        orchestrator = composition.evaluation_orchestrator()
        run_batch_ref = next(
            (ref for ref in req.input_refs if ref.entity_type == "run_batch"), None
        )
        preprocess_batch_ref = next(
            (ref for ref in req.input_refs if ref.entity_type == "preprocess_batch"),
            None,
        )
        if run_batch_ref is None or preprocess_batch_ref is None:
            return StageExecutionResult(
                status="error",
                output_refs=[],
                errors=[
                    {
                        "code": "xmax.missing_input",
                        "message": "evaluate requires both run_batch and preprocess_batch",
                        "stage": "evaluate",
                        "retryable": False,
                    }
                ],
            )
        streamed = bool(
            stream_state
            and stream_state.get("active")
            and stream_state.get("run_batch_id") == run_batch_ref.entity_id
        )
        if streamed:
            results = list(stream_state.get("evaluations", []))
            errors = list(stream_state.get("errors", {}).get("evaluate", []))
            evaluation_batch_id = stream_state["evaluation_batch_id"]
            runs = composition.database.list_runs(run_batch_id=run_batch_ref.entity_id)
            results = orchestrator.finalize_batch_context(runs, results)
            aggregate = aggregate_evaluation_results(results)
            manifest = build_batch_manifest(
                entity_type="evaluation_batch",
                item_entity_type="evaluation_result",
                item_ids=[item["evaluation_id"] for item in results],
                producer_stage_run_id=req.stage_run_id,
                batch_id=evaluation_batch_id,
                metadata={
                    **stream_state.get("metadata", {}),
                    "execution_mode": "streaming",
                    "streamed_during_generation": True,
                    "score_source": "criterion_results",
                    "aggregate": aggregate,
                },
            )
            return StageExecutionResult(
                status="partial" if errors else "completed",
                output_refs=[],
                batch_manifests=[manifest],
                errors=errors,
                metadata=manifest["metadata"],
            )
        runs = composition.database.list_runs(run_batch_id=run_batch_ref.entity_id)
        preprocess_manifest = composition.database.get_batch_manifest(
            "preprocess_batch", preprocess_batch_ref.entity_id
        )
        preprocess_by_run = {}
        for preprocess_id in preprocess_manifest.get("item_ids", []):
            item = composition.database.get_preprocess_run(preprocess_id)
            preprocess_by_run[item["run_id"]] = item
        missing = [run["run_id"] for run in runs if run["run_id"] not in preprocess_by_run]
        if missing:
            return StageExecutionResult(
                status="error",
                output_refs=[],
                errors=[
                    {
                        "code": "xmax.missing_input",
                        "message": f"preprocess_batch does not cover runs: {missing}",
                        "stage": "evaluate",
                        "retryable": False,
                    }
                ],
            )
        summary = orchestrator.evaluate_runs(runs, preprocess_by_run)
        manifest = build_batch_manifest(
            entity_type="evaluation_batch",
            item_entity_type="evaluation_result",
            item_ids=[r["evaluation_id"] for r in summary.get("results", [])],
            producer_stage_run_id=req.stage_run_id,
            batch_id=summary.get("evaluation_batch_id"),
            metadata={
                "execution_mode": "batch",
                "score_source": "criterion_results",
                "aggregate": summary.get("aggregate", {}),
            },
        )
        return StageExecutionResult(
            status="completed" if not summary.get("errors") else "partial",
            output_refs=[],
            batch_manifests=[manifest],
            errors=summary.get("errors", []),
        )

    return _stage_wrapper(PipelineStage.EVALUATE, execute)


def _report_executor(composition: Composition, request: dict[str, Any], args: argparse.Namespace):
    from .contracts import PipelineStage
    from .pipeline.models import StageExecutionRequest, StageExecutionResult

    def execute(req: StageExecutionRequest) -> StageExecutionResult:
        if req.dry_run:
            return StageExecutionResult(
                status="completed",
                output_refs=[],
                metadata={"dry_run": True, "report": "validated_only"},
            )
        comparison = request.get("comparison", {})
        service = composition.reporting_service()
        result = service.generate(
            comparison_id=comparison.get("comparison_id", "model-update"),
            baseline_model_version=comparison.get("baseline_model_version", ""),
            candidate_model_version=comparison.get("candidate_model_version", ""),
            baseline_run_batch_id=comparison.get("baseline_run_batch_id", ""),
            candidate_run_batch_id=comparison.get("candidate_run_batch_id", ""),
            baseline_evaluation_batch_id=comparison.get("baseline_evaluation_batch_id", ""),
            candidate_evaluation_batch_id=comparison.get("candidate_evaluation_batch_id", ""),
            requested_scene_ids=comparison.get("requested_scene_ids", []),
            template_path=composition._path(
                "report_template_path",
                "report-templates/model-version-update-report.md",
            )
            if "report_template_path" in comparison
            else composition.root
            / comparison.get(
                "report_template_path",
                "report-templates/model-version-update-report.md",
            ),
            output_directory=composition._path(
                "report_output_directory", "var/reports/model-version-updates"
            )
            if "report_output_directory" in comparison
            else composition.root
            / comparison.get("report_output_directory", "var/reports/model-version-updates"),
        )
        return StageExecutionResult(status="completed", output_refs=[], metadata=result)

    return _stage_wrapper(PipelineStage.REPORT, execute)


def _sync_executor(composition: Composition, request: dict[str, Any], args: argparse.Namespace):
    from .contracts import PipelineStage
    from .pipeline.manifests import build_batch_manifest
    from .pipeline.models import StageExecutionRequest, StageExecutionResult

    def execute(req: StageExecutionRequest) -> StageExecutionResult:
        if req.dry_run:
            return StageExecutionResult(
                status="completed",
                output_refs=[],
                metadata={
                    "dry_run": True,
                    "policy": request.get("sync_policy", "full"),
                },
            )
        policy = request.get("sync_policy", "full")
        service = composition.feishu_sync_service()
        runs = []
        evaluations: dict[str, dict[str, Any]] = {}
        for ref in req.input_refs:
            if ref.entity_type == "evaluation_batch":
                results = composition.database.list_evaluation_results(
                    evaluation_batch_id=ref.entity_id
                )
                for item in results:
                    previous = evaluations.get(item["run_id"])
                    if previous and previous.get("evaluation_id") != item.get("evaluation_id"):
                        return StageExecutionResult(
                            status="error",
                            output_refs=[],
                            errors=[
                                {
                                    "code": "xmax.ambiguous_input",
                                    "message": (
                                        f"multiple selected EvaluationResults for Run {item['run_id']}: "
                                        f"{previous.get('evaluation_id')} and {item.get('evaluation_id')}"
                                    ),
                                    "stage": "sync",
                                    "retryable": False,
                                }
                            ],
                        )
                    evaluations[item["run_id"]] = item
                runs.extend(composition.database.get_run(item["run_id"]) for item in results)
            elif ref.entity_type == "run_batch":
                manifest = composition.database.get_batch_manifest("run_batch", ref.entity_id)
                runs.extend(
                    composition.database.get_run(run_id) for run_id in manifest.get("item_ids", [])
                )
        runs = list({run["run_id"]: run for run in runs}.values())
        if not runs:
            return StageExecutionResult(
                status="error",
                output_refs=[],
                errors=[
                    {
                        "code": "xmax.missing_input",
                        "message": "sync selector resolved to no generation runs",
                        "stage": "sync",
                        "retryable": False,
                    }
                ],
            )
        summary = service.sync_case_runs(
            runs, evaluations=evaluations, policy=policy, dry_run=args.dry_run
        )
        manifest = build_batch_manifest(
            entity_type="sync_batch",
            item_entity_type="generation_run",
            item_ids=[run["run_id"] for run in runs],
            producer_stage_run_id=req.stage_run_id,
            source_batch_refs=[ref.__dict__ for ref in req.input_refs],
            metadata={
                "policy": policy,
                "sync_summary": summary,
                "selected_evaluation_ids": sorted(
                    item.get("evaluation_id", "")
                    for item in evaluations.values()
                    if item.get("evaluation_id")
                ),
            },
        )
        return StageExecutionResult(
            status="completed" if not summary["errors"] else "partial",
            output_refs=[],
            batch_manifests=[manifest],
            errors=summary["errors"],
            metadata={"sync_summary": summary},
        )

    return _stage_wrapper(PipelineStage.SYNC, execute)


def _reconcile_executor(composition: Composition, request: dict[str, Any]):
    from .contracts import PipelineStage
    from .pipeline.models import StageExecutionRequest, StageExecutionResult

    def execute(req: StageExecutionRequest) -> StageExecutionResult:
        if req.dry_run:
            return StageExecutionResult(
                status="completed",
                output_refs=[],
                metadata={"dry_run": True, "reconcile": "skipped in dry-run"},
            )
        sync_ref = next((ref for ref in req.input_refs if ref.entity_type == "sync_batch"), None)
        if sync_ref is None:
            return StageExecutionResult(
                status="error",
                output_refs=[],
                errors=[
                    {
                        "code": "xmax.missing_input",
                        "message": "reconcile requires a sync_batch",
                        "stage": "reconcile",
                        "retryable": False,
                    }
                ],
            )
        sync_manifest = composition.database.get_batch_manifest("sync_batch", sync_ref.entity_id)
        selected = {
            item["run_id"]: item
            for evaluation_id in sync_manifest.get("metadata", {}).get(
                "selected_evaluation_ids", []
            )
            for item in [composition.database.get_evaluation_result(evaluation_id)]
        }
        outcome = composition.feishu_reconcile().reconcile(
            run_ids=sync_manifest.get("item_ids", []),
            evaluations=selected,
        )
        return StageExecutionResult(
            status="completed" if outcome.get("ok") else "partial",
            output_refs=[],
            metadata={"reconcile": outcome},
            errors=[]
            if outcome.get("ok")
            else [
                {
                    "code": "xmax.conflict",
                    "message": "reconcile differences found",
                    "stage": "reconcile",
                    "retryable": True,
                }
            ],
        )

    return _stage_wrapper(PipelineStage.RECONCILE, execute)


def _stage_wrapper(stage: Any, fn: Callable[[Any], Any]) -> Any:
    stage_value = stage

    class Wrapper:
        stage = stage_value

        def execute(self, request: Any) -> Any:
            return fn(request)

    return Wrapper()


# ----------------------------------------------------------------------
# standalone stage commands
# ----------------------------------------------------------------------
def cmd_ingest_assets(composition: Composition, args: argparse.Namespace) -> int:
    services = composition.ingest_service()
    if args.asset_action == "verify":
        manifest = composition.database.get_batch_manifest("asset_batch", args.batch_id)
        data = services["runner"].verify_batch(manifest.get("item_ids", []))
        _emit(args, "ingest.assets.verify", data)
        return 0 if not any("error" in item for item in data["results"]) else EXIT_PARTIAL
    config_path = composition.root / args.config
    sources = load_config(config_path, "asset-sources.schema.json")
    if args.asset_action == "discover":
        data = {"discovered": services["runner"].discover(sources)}
    else:
        data = services["runner"].sync(sources)
    _emit(args, f"ingest.assets.{args.asset_action}", data, ok=not data.get("errors"))
    return 0 if not data.get("errors") else EXIT_PARTIAL


def cmd_ingest_results(composition: Composition, args: argparse.Namespace) -> int:
    services = composition.ingest_service()
    config = load_config(composition.root / args.config, "existing-results.schema.json")
    outcome = services["importer"].import_results(
        config, feishu_client=composition.feishu_client(read_only=True)
    )
    data = outcome.to_dict()
    _emit(args, "ingest.results", data, ok=outcome.status in {"completed", "skipped"})
    return 0 if outcome.status in {"completed", "skipped"} else EXIT_PARTIAL


def cmd_plan(composition: Composition, args: argparse.Namespace) -> int:
    if args.action == "show":
        data = composition.database.get_test_plan(args.plan_id)
        _emit(args, "plan.show", data)
        return 0
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    builder = composition.plan_builder()
    plan_request = {
        "asset_batch_ids": [],
        "model_id": composition.project.get("default_model", "x2.0"),
        "repeat_count": request.get("repeat_count", composition.project.get("default_repeats", 5)),
        "generation_modes": request.get("generation_modes", ["offline"]),
        "generation_mode_overrides": request.get("generation_mode_overrides", []),
        "filters": request.get("filters", {}),
        "seed": request.get("seed"),
        "combination_selection": request.get("combination_selection", {"strategy": "cartesian"}),
        "generation_config": {
            "quality": composition.project.get("xmax_offline_quality", "hd"),
            "fps": composition.project.get("xmax_offline_fps", 24),
            "duration_s": 3,
            **request.get("generation_config", {}),
        },
    }
    if args.action == "preview":
        data = builder.preview(plan_request)
    else:
        data = builder.build(plan_request)
        from .pipeline.manifests import ManifestStore, build_batch_manifest

        task_manifest = build_batch_manifest(
            entity_type="task_batch",
            item_entity_type="test_task",
            item_ids=data["task_ids"],
            producer_stage_run_id="cli-plan",
            batch_id=data["task_batch_id"],
            metadata={"plan_id": data["plan_id"]},
        )
        ManifestStore(composition.manifest_root, composition.database).save_batch_manifest(
            task_manifest
        )
    _emit(args, f"plan.{args.action}", data)
    return 0


def cmd_task(composition: Composition, args: argparse.Namespace) -> int:
    """Inspect, claim, or execute one frozen task."""

    if args.action == "show":
        data = composition.database.get_test_task(args.task_id)
        _emit(args, "task.show", data)
        return 0
    if args.action == "claim":
        data = composition.database.claim_next_test_task(
            args.task_batch_id,
            args.lease_owner,
            lease_seconds=args.lease_seconds,
        )
        _emit(args, "task.claim", {"task": data})
        return 0
    if not args.budget_approved:
        from .errors import ApprovalRequiredError

        raise ApprovalRequiredError(
            "task execution may call paid generation; preview the plan and pass --budget-approved"
        )
    from .tasks import TaskWorker
    from .tasks.runtime import PipelineTaskRuntime

    runtime = PipelineTaskRuntime(
        composition,
        lease_owner=args.lease_owner,
        sync_policy=args.sync_policy,
        reconcile=not args.no_reconcile,
        headed=args.headed,
    )
    data = TaskWorker(composition.database, runtime).run_task(
        args.task_id,
        lease_owner=args.lease_owner,
        lease_seconds=args.lease_seconds,
        resume=args.resume,
    )
    _emit(args, "task.run", data, ok=data.get("status") == "completed")
    if data.get("status") == "evaluation_paused":
        if (data.get("last_error") or {}).get("pause_scope") == "evaluation_batch":
            return EXIT_EXTERNAL_FAILURE
        return EXIT_APPROVAL_REQUIRED
    return 0 if data.get("status") == "completed" else EXIT_PARTIAL


def cmd_worker(composition: Composition, args: argparse.Namespace) -> int:
    """Repeatedly consume frozen tasks until empty or max_tasks is reached."""

    if args.action == "status":
        data = composition.database.test_task_summary(args.task_batch_id)
        _emit(args, "worker.status", data)
        return 0
    if not args.budget_approved:
        from .errors import ApprovalRequiredError

        raise ApprovalRequiredError(
            "worker execution may call paid generation; preview the plan and pass --budget-approved"
        )
    from .tasks import TaskWorker
    from .tasks.runtime import PipelineTaskRuntime

    runtime = PipelineTaskRuntime(
        composition,
        lease_owner=args.lease_owner,
        sync_policy=args.sync_policy,
        reconcile=not args.no_reconcile,
        headed=args.headed,
    )
    data = TaskWorker(composition.database, runtime).run_batch(
        args.task_batch_id,
        lease_owner=args.lease_owner,
        lease_seconds=args.lease_seconds,
        max_tasks=args.max_tasks,
    )
    ok = not data.get("errors") and not data.get("evaluation_paused")
    _emit(args, "worker.run", data, ok=ok)
    if data.get("evaluation_paused"):
        if any(
            item.get("pause_scope") == "evaluation_batch"
            for item in data["evaluation_paused"]
        ):
            return EXIT_EXTERNAL_FAILURE
        return EXIT_APPROVAL_REQUIRED
    return 0 if ok else EXIT_PARTIAL


def cmd_generate_offline(composition: Composition, args: argparse.Namespace) -> int:
    if not args.budget_approved:
        from .errors import ApprovalRequiredError

        raise ApprovalRequiredError(
            "standalone offline generation requires --budget-approved after plan preview"
        )
    plan = composition.database.get_test_plan(args.plan_id)
    model_id = composition.project.get("default_model", "x2.0")
    batch_id = f"runs-{plan.get('plan_hash', '')[:12]}"
    adapter = composition.offline_adapter(run_batch_id=batch_id, model_id=model_id)
    if any(case.get("generation_mode") == "offline" for case in plan.get("cases", [])):
        adapter.preflight()
    run_ids = []
    for case in plan.get("cases", []):
        if case.get("generation_mode") != "offline":
            continue
        if args.resume:
            existing = composition.database.list_runs(case_id=case["case_id"], status="completed")
            if existing:
                run_ids.append(existing[0]["run_id"])
                continue
        run = adapter.run_case(case)
        run_ids.append(run["run_id"])
    for run_id in run_ids:
        composition.database.set_run_batch_id(run_id, batch_id)
    from .pipeline.manifests import build_batch_manifest

    manifest = build_batch_manifest(
        entity_type="run_batch",
        item_entity_type="generation_run",
        item_ids=run_ids,
        producer_stage_run_id="cli-generate",
    )
    composition.database.save_batch_manifest(manifest)
    _emit(args, "generate.offline", {"run_batch_id": batch_id, "run_ids": run_ids})
    return 0


def cmd_generate_realtime(composition: Composition, args: argparse.Namespace) -> int:
    if not args.budget_approved:
        from .errors import ApprovalRequiredError

        raise ApprovalRequiredError(
            "standalone realtime generation requires --budget-approved after plan preview"
        )
    plan = composition.database.get_test_plan(args.plan_id)
    model_id = composition.project.get("default_model", "x2.0")
    batch_id = f"runs-{plan.get('plan_hash', '')[:12]}"
    controller = composition.realtime_controller(
        run_batch_id=batch_id, model_id=model_id, headed=args.headed
    )
    run_ids: list[str] = []
    errors: list[dict[str, Any]] = []
    for case in plan.get("cases", []):
        if case.get("generation_mode") != "realtime":
            continue
        if args.resume:
            existing = composition.database.list_runs(case_id=case["case_id"], status="completed")
            if existing:
                run_ids.append(existing[0]["run_id"])
                continue
        # A single realtime case must not abort the whole 250-case batch; the
        # browser SDK or one feed can fail (media quirk, transient network,
        # RTC hiccup) while the remaining cases are still valid.  Record the
        # failure and keep draining; re-running with --resume only retries the
        # missing/error cases.
        try:
            run = controller.run_case(case, composition.realtime_case_config(case, headed=args.headed))
        except Exception as exc:
            errors.append(
                {
                    "code": "xmax.realtime_case_error",
                    "message": f"realtime case {case['case_id']} failed: {exc}",
                    "stage": "generate",
                    "retryable": True,
                    "entity_id": case["case_id"],
                }
            )
            continue
        run_ids.append(run["run_id"])
    for run_id in run_ids:
        composition.database.set_run_batch_id(run_id, batch_id)
    from .pipeline.manifests import build_batch_manifest

    manifest = build_batch_manifest(
        entity_type="run_batch",
        item_entity_type="generation_run",
        item_ids=run_ids,
        producer_stage_run_id="cli-generate-rt",
        errors=errors,
        metadata={"realtime_case_errors": len(errors)},
    )
    composition.database.save_batch_manifest(manifest)
    _emit(args, "generate.realtime", {"run_batch_id": batch_id, "run_ids": run_ids, "errors": errors})
    return 0 if not errors else EXIT_PARTIAL


def cmd_preprocess(composition: Composition, args: argparse.Namespace) -> int:
    service = composition.preprocess_service()
    preprocess_ids = []
    for run in composition.database.list_runs(run_batch_id=args.run_batch_id):
        result = service.build(run)
        preprocess_ids.append(result["preprocess_id"])
    from .pipeline.manifests import build_batch_manifest

    manifest = build_batch_manifest(
        entity_type="preprocess_batch",
        item_entity_type="preprocess_run",
        item_ids=preprocess_ids,
        producer_stage_run_id="cli-preprocess",
    )
    composition.database.save_batch_manifest(manifest)
    _emit(
        args,
        "preprocess",
        {
            "run_batch_id": args.run_batch_id,
            "preprocess_batch_id": manifest["batch_id"],
            "preprocess_ids": preprocess_ids,
        },
    )
    return 0


def cmd_evaluate(composition: Composition, args: argparse.Namespace) -> int:
    orchestrator = composition.evaluation_orchestrator()
    runs = composition.database.list_runs(run_batch_id=args.run_batch_id)
    summary = orchestrator.evaluate_runs(runs, resume=args.resume)
    _emit(args, "evaluate", summary, ok=not summary.get("errors"))
    return 0 if not summary.get("errors") else EXIT_PARTIAL


def cmd_evaluation_budget(composition: Composition, args: argparse.Namespace) -> int:
    """Inspect, pause, or explicitly authorize the project-local paid budget."""

    from .errors import ApprovalRequiredError

    gate = composition.evaluation_budget_gate()
    budget_id = gate.policy.budget_id
    if args.budget_id and args.budget_id != budget_id:
        raise ConfigError(
            f"configured evaluation budget is {budget_id}, not {args.budget_id}"
        )
    if args.action == "authorize":
        if not args.recharge_confirmed:
            raise ApprovalRequiredError(
                "refusing to open paid MLLM evaluation without --recharge-confirmed"
            )
        state = gate.authorize(operator=args.operator, limit_cny=args.limit_cny)
    elif args.action == "pause":
        state = gate.pause(args.reason)
    else:
        state = gate.status()
    data = {
        "budget": state,
        "accounting_scope": "this xmax-test database only",
        "warning": (
            "This is a conservative local estimate from provider usage; Alibaba account-wide "
            "charges and calls made outside this project are not included."
        ),
        "next_action": (
            "Keep FreeTierOnly enabled for every earlier model. Disable it only for the final "
            "qwen3-vl-flash alias, recharge, then authorize this budget."
        ),
    }
    _emit(args, f"evaluation-budget.{args.action}", data)
    return 0


def cmd_sync(composition: Composition, args: argparse.Namespace) -> int:
    service = composition.feishu_sync_service()
    runs: list[dict[str, Any]] = []
    evaluations: dict[str, dict[str, Any]] = {}
    if args.evaluation_batch_id:
        results = composition.database.list_evaluation_results(
            evaluation_batch_id=args.evaluation_batch_id
        )
        evaluations = {item["run_id"]: item for item in results}
        runs = [composition.database.get_run(item["run_id"]) for item in results]
        selector_label = args.evaluation_batch_id
    elif args.run_batch_id:
        runs = composition.database.list_runs(run_batch_id=args.run_batch_id)
        selector_label = args.run_batch_id
    else:
        selector_path = composition.root / args.selector
        if selector_path.is_file():
            from .pipeline.selectors import SelectorResolver

            selector = json.loads(selector_path.read_text(encoding="utf-8"))
            frozen = SelectorResolver(composition.database, composition.manifest_root).resolve(
                selector
            )
            entity_type = frozen["entity_type"]
            for entity_id in frozen["resolved_entity_ids"]:
                if entity_type == "evaluation_batch":
                    results = composition.database.list_evaluation_results(
                        evaluation_batch_id=entity_id
                    )
                    for item in results:
                        previous = evaluations.get(item["run_id"])
                        if previous and previous.get("evaluation_id") != item.get("evaluation_id"):
                            raise XmaxTestError(
                                f"multiple selected EvaluationResults for Run {item['run_id']}: "
                                f"{previous.get('evaluation_id')} and {item.get('evaluation_id')}"
                            )
                        evaluations[item["run_id"]] = item
                    runs.extend(composition.database.get_run(item["run_id"]) for item in results)
                elif entity_type == "run_batch":
                    runs.extend(composition.database.list_runs(run_batch_id=entity_id))
                else:
                    raise XmaxTestError(
                        f"sync selector must target run_batch or evaluation_batch, got {entity_type}"
                    )
            selector_label = args.selector
        else:
            selector_label = args.selector
            if args.selector.startswith("eval"):
                results = composition.database.list_evaluation_results(
                    evaluation_batch_id=args.selector
                )
                evaluations = {item["run_id"]: item for item in results}
                runs = [composition.database.get_run(item["run_id"]) for item in results]
            else:
                runs = composition.database.list_runs(run_batch_id=args.selector)
    runs = list({item["run_id"]: item for item in runs}.values())
    if not runs:
        raise XmaxTestError(f"sync selector matched no runs: {selector_label}")
    summary = service.sync_case_runs(
        runs,
        evaluations=evaluations,
        policy=args.policy,
        dry_run=args.action == "dry-run",
    )
    _emit(args, "sync", summary, ok=not summary["errors"])
    return 0 if not summary["errors"] else EXIT_PARTIAL


def cmd_reconcile(composition: Composition, args: argparse.Namespace) -> int:
    if args.sync_batch_id:
        manifest = composition.database.get_batch_manifest("sync_batch", args.sync_batch_id)
        selected = {
            item["run_id"]: item
            for evaluation_id in manifest.get("metadata", {}).get("selected_evaluation_ids", [])
            for item in [composition.database.get_evaluation_result(evaluation_id)]
        }
        outcome = composition.feishu_reconcile().reconcile(
            run_ids=manifest.get("item_ids", []), evaluations=selected
        )
    else:
        outcome = composition.feishu_reconcile().reconcile(run_batch_id=args.run_batch_id)
    _emit(args, "reconcile", outcome, ok=outcome.get("ok", False))
    return 0 if outcome.get("ok") else EXIT_PARTIAL


def cmd_stage_show(composition: Composition, args: argparse.Namespace) -> int:
    manifest = composition.database.get_stage_run(args.stage_run_id)
    _emit(args, "stage.show", {"manifest": manifest})
    return 0


def cmd_batch_show(composition: Composition, args: argparse.Namespace) -> int:
    entity_type = _guess_entity_type(args.batch_id)
    manifest = composition.database.get_batch_manifest(entity_type, args.batch_id)
    _emit(args, "batch.show", {"manifest": manifest})
    return 0


def _guess_entity_type(batch_id: str) -> str:
    prefix_map = {
        "assets": "asset_batch",
        "plan": "test_plan",
        "tasks": "task_batch",
        "runs": "run_batch",
        "prep": "preprocess_batch",
        "eval": "evaluation_batch",
        "signals": "human_signal_batch",
        "report": "report_bundle",
        "sync": "sync_batch",
    }
    for prefix, entity_type in prefix_map.items():
        if batch_id.startswith(prefix):
            return entity_type
    return "run_batch"


def cmd_report(composition: Composition, args: argparse.Namespace) -> int:
    if args.action == "single-version":
        request = load_config(
            composition.root / args.request,
            "single-version-report-request.schema.json",
            base_dir=composition.root,
        )
        result = composition.single_version_reporting_service().generate(
            report_id=request["report_id"],
            model_version=request["model_version"],
            run_batch_id=request["run_batch_id"],
            evaluation_batch_id=request["evaluation_batch_id"],
            requested_scene_ids=request.get("requested_scene_ids"),
            output_directory=composition.root
            / request.get("output_directory", "var/reports/single-version"),
        )
        _emit(args, "report.single-version", result)
        return 0
    request = json.loads((composition.root / args.request).read_text(encoding="utf-8"))
    comparison = request.get("comparison", {})
    service = composition.reporting_service()
    result = service.generate(
        comparison_id=comparison.get("comparison_id", "model-update"),
        baseline_model_version=comparison.get("baseline_model_version", ""),
        candidate_model_version=comparison.get("candidate_model_version", ""),
        baseline_run_batch_id=comparison.get("baseline_run_batch_id", ""),
        candidate_run_batch_id=comparison.get("candidate_run_batch_id", ""),
        baseline_evaluation_batch_id=comparison.get("baseline_evaluation_batch_id", ""),
        candidate_evaluation_batch_id=comparison.get("candidate_evaluation_batch_id", ""),
        requested_scene_ids=comparison.get("requested_scene_ids", []),
        template_path=composition.root
        / comparison.get("report_template_path", "report-templates/model-version-update-report.md"),
        output_directory=composition.root
        / comparison.get("report_output_directory", "var/reports/model-version-updates"),
    )
    _emit(args, "report.model-update", result)
    return 0


def cmd_judges(composition: Composition, args: argparse.Namespace) -> int:
    if args.action == "list":
        data = {
            "judges": [
                {
                    "judge_id": item.judge_id,
                    "version": item.version,
                    "manifest": item.manifest,
                }
                for item in sorted(
                    [item for item in composition.judges._judges.values()],
                    key=lambda i: (i.judge_id, i.version),
                )
            ]
        }
    elif args.action == "check":
        data = {"ok": True, "message": "judge registry loaded"}
    elif args.action == "run":
        context = json.loads((composition.root / args.context).read_text(encoding="utf-8"))
        registered = composition.judges.get(args.judge_id, args.version)
        judgments = registered.plugin.evaluate(context)
        data = {
            "judge_id": args.judge_id,
            "version": args.version,
            "judgments": judgments,
        }
    elif args.action == "promote":
        from .judges.releases import JudgeReleaseService

        validation = json.loads((composition.root / args.validation).read_text(encoding="utf-8"))
        data = JudgeReleaseService(composition.database).promote(
            args.judge_id, args.version, validation
        )
    else:
        from .judges.releases import JudgeReleaseService

        data = JudgeReleaseService(composition.database).rollback(
            args.judge_id, args.version, args.reason
        )
    _emit(args, f"judges.{args.action}", data)
    return 0


# ----------------------------------------------------------------------
# output + parser
# ----------------------------------------------------------------------
def _emit(args: argparse.Namespace, command: str, data: dict[str, Any], ok: bool = True) -> None:
    if getattr(args, "json", False):
        payload = {
            "ok": ok,
            "command": command,
            "status": "complete" if ok else "error",
            "data": redact(data),
            "warnings": [],
            "errors": data.get("errors", []),
            "artifact_paths": [],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(redact(data), ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xmax-test")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p = subparsers.add_parser("project-check", help="verify required project files")

    p = subparsers.add_parser(
        "benchmark-check", help="validate the contract embedded in BENCHMARK.md"
    )
    p.add_argument("--path", type=Path, default=None)
    p.add_argument("--scenario-pack", type=Path, default=None)

    p = subparsers.add_parser("db", help="database migration and checks")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("migrate")
    sub.add_parser("check")

    p = subparsers.add_parser("artifacts", help="artifact store operations")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("verify")
    sub.add_parser("gc").add_argument("--dry-run", action="store_true")

    p = subparsers.add_parser("context-check", help="one-shot context check")
    p.add_argument("--request", required=True)

    p = subparsers.add_parser("run", help="unified pipeline run")
    p.add_argument("--request", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--smoke-limit", type=int)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--budget-approved", action="store_true")

    p = subparsers.add_parser("ingest", help="ingest assets or existing results")
    sub = p.add_subparsers(dest="action", required=True)
    assets_parser = sub.add_parser("assets")
    asset_sub = assets_parser.add_subparsers(dest="asset_action", required=True)
    for asset_action in ("discover", "sync"):
        asset_sub.add_parser(asset_action).add_argument("--config", required=True)
    asset_sub.add_parser("verify").add_argument("--batch-id", required=True)
    sub.add_parser("results").add_argument("--config", required=True)

    p = subparsers.add_parser("plan", help="plan preview/build/show")
    sub = p.add_subparsers(dest="action", required=True)
    for action in ("preview", "build"):
        sub.add_parser(action).add_argument("--request", required=True)
    sub.add_parser("show").add_argument("--plan-id", required=True)

    p = subparsers.add_parser("generate", help="generate offline/realtime")
    sub = p.add_subparsers(dest="action", required=True)
    offline_parser = sub.add_parser("offline")
    offline_parser.add_argument("--plan-id", required=True)
    offline_parser.add_argument("--resume", action="store_true")
    offline_parser.add_argument("--budget-approved", action="store_true")
    realtime_parser = sub.add_parser("realtime")
    realtime_parser.add_argument("--plan-id", required=True)
    realtime_parser.add_argument("--headed", action="store_true")
    realtime_parser.add_argument("--resume", action="store_true")
    realtime_parser.add_argument("--budget-approved", action="store_true")

    p = subparsers.add_parser("task", help="inspect, claim, or run one frozen test task")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("show").add_argument("--task-id", required=True)
    claim_parser = sub.add_parser("claim")
    claim_parser.add_argument("--task-batch-id", required=True)
    claim_parser.add_argument("--lease-owner", required=True)
    claim_parser.add_argument("--lease-seconds", type=int, default=900)
    task_run = sub.add_parser("run")
    task_run.add_argument("--task-id", required=True)
    task_run.add_argument("--lease-owner", required=True)
    task_run.add_argument("--lease-seconds", type=int, default=900)
    task_run.add_argument(
        "--sync-policy",
        choices=["none", "score_only", "metadata_only", "attachments_only", "full"],
        default="full",
    )
    task_run.add_argument("--no-reconcile", action="store_true")
    task_run.add_argument("--headed", action="store_true")
    task_run.add_argument("--resume", action="store_true")
    task_run.add_argument("--budget-approved", action="store_true")

    p = subparsers.add_parser("worker", help="consume a frozen task batch")
    sub = p.add_subparsers(dest="action", required=True)
    worker_status = sub.add_parser("status")
    worker_status.add_argument("--task-batch-id", required=True)
    worker_run = sub.add_parser("run")
    worker_run.add_argument("--task-batch-id", required=True)
    worker_run.add_argument("--lease-owner", required=True)
    worker_run.add_argument("--lease-seconds", type=int, default=900)
    worker_run.add_argument("--max-tasks", type=int)
    worker_run.add_argument(
        "--sync-policy",
        choices=["none", "score_only", "metadata_only", "attachments_only", "full"],
        default="full",
    )
    worker_run.add_argument("--no-reconcile", action="store_true")
    worker_run.add_argument("--headed", action="store_true")
    worker_run.add_argument("--budget-approved", action="store_true")

    p = subparsers.add_parser("preprocess", help="build preprocessing evidence")
    p.add_argument("--run-batch-id", required=True)

    p = subparsers.add_parser("evaluate", help="evaluate a completed run batch")
    p.add_argument("--run-batch-id", required=True)
    p.add_argument("--resume", action="store_true")

    p = subparsers.add_parser(
        "evaluation-budget", help="inspect or explicitly reopen the paid MLLM budget"
    )
    sub = p.add_subparsers(dest="action", required=True)
    budget_status = sub.add_parser("status")
    budget_status.add_argument("--budget-id")
    budget_authorize = sub.add_parser("authorize")
    budget_authorize.add_argument("--budget-id")
    budget_authorize.add_argument("--limit-cny", type=float, default=99.0)
    budget_authorize.add_argument("--operator", required=True)
    budget_authorize.add_argument("--recharge-confirmed", action="store_true")
    budget_pause = sub.add_parser("pause")
    budget_pause.add_argument("--budget-id")
    budget_pause.add_argument("--reason", required=True)

    p = subparsers.add_parser("human", help="human signal operations")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("import").add_argument("--input", required=True)
    sub.add_parser("feedback").add_argument("--input", required=True)
    sub.add_parser("import-media").add_argument("--input", required=True)
    sub.add_parser("import-feishu").add_argument("--config", required=True)
    normalize_parser = sub.add_parser("normalize")
    normalize_parser.add_argument("--pending", action="store_true")
    normalize_parser.add_argument("--signal-id", action="append", default=[])
    normalize_parser.add_argument("--rule-based", action="store_true")
    normalize_parser.add_argument("--workers", type=int, default=1)
    partition_parser = sub.add_parser("partition")
    partition_parser.add_argument("--output", required=True)
    partition_parser.add_argument("--repartition", action="store_true")
    override_parser = sub.add_parser("apply-overrides")
    override_parser.add_argument("--signal-id", action="append", default=[])
    challenger_parser = sub.add_parser("build-challenger")
    challenger_parser.add_argument("--judge-id", required=True)
    challenger_parser.add_argument("--version", required=True)
    challenger_parser.add_argument("--route", choices=["cv", "mlmm", "fusion"], required=True)
    challenger_parser.add_argument("--train", required=True)
    challenger_parser.add_argument("--output-directory", default="var/learning/challengers")
    challenger_parser.add_argument("--trainer-entrypoint")
    challenger_parser.add_argument("--base-version")

    p = subparsers.add_parser("report", help="generate reports")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("model-update").add_argument("--request", required=True)
    sub.add_parser("single-version").add_argument("--request", required=True)

    p = subparsers.add_parser("sync", help="feishu sync")
    sub = p.add_subparsers(dest="action", required=True)
    for sync_action in ("dry-run", "run"):
        sync_parser = sub.add_parser(sync_action)
        selector_group = sync_parser.add_mutually_exclusive_group(required=True)
        selector_group.add_argument("--selector")
        selector_group.add_argument("--evaluation-batch-id")
        selector_group.add_argument("--run-batch-id")
        sync_parser.add_argument("--policy", default="full")

    p = subparsers.add_parser("reconcile", help="feishu reconcile")
    reconcile_selector = p.add_mutually_exclusive_group()
    reconcile_selector.add_argument("--sync-batch-id")
    reconcile_selector.add_argument("--run-batch-id")

    p = subparsers.add_parser("stage", help="stage inspection")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("show").add_argument("--stage-run-id", required=True)

    p = subparsers.add_parser("batch", help="batch inspection")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("show").add_argument("--batch-id", required=True)

    p = subparsers.add_parser("judges", help="judge registry operations")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    sub.add_parser("check")
    judge_run = sub.add_parser("run")
    judge_run.add_argument("--judge-id", required=True)
    judge_run.add_argument("--version", required=True)
    judge_run.add_argument("--context", required=True)
    judge_promote = sub.add_parser("promote")
    judge_promote.add_argument("--judge-id", required=True)
    judge_promote.add_argument("--version", required=True)
    judge_promote.add_argument("--validation", required=True)
    judge_rollback = sub.add_parser("rollback")
    judge_rollback.add_argument("--judge-id", required=True)
    judge_rollback.add_argument("--version", required=True)
    judge_rollback.add_argument("--reason", required=True)

    p = subparsers.add_parser("replay", help="replay old runs with new benchmark")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("run").add_argument("--run-batch-id", required=True)

    p = subparsers.add_parser("release", help="benchmark/judge release")
    sub = p.add_subparsers(dest="action", required=True)
    validate_release = sub.add_parser("validate")
    validate_release.add_argument("--holdout", required=True)
    validate_release.add_argument("--threshold", type=float, default=0.5)
    promote_release = sub.add_parser("promote")
    promote_release.add_argument("--validation", required=True)
    promote_release.add_argument("--operator", required=True)
    rollback_release = sub.add_parser("rollback")
    rollback_release.add_argument("--previous-version", required=True)
    rollback_release.add_argument("--operator", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    # --root may appear before or after the subcommand; extract it globally.
    root = Path(".")
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--root":
            if index + 1 < len(argv):
                root = Path(argv[index + 1])
                index += 2
                continue
            index += 1
            continue
        remaining.append(token)
        index += 1
    argv = remaining
    args = build_parser().parse_args(argv)
    if hasattr(args, "project_root") and args.project_root:
        root = args.project_root
    composition = Composition(root)
    try:
        handler = _HANDLERS.get(args.command)
        if handler is None:
            raise AssertionError(f"unhandled command: {args.command}")
        return handler(composition, args)
    except XmaxTestError as exc:
        _emit(
            args,
            getattr(args, "command", "xmax"),
            {"errors": [exc.to_dict()]},
            ok=False,
        )
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - defensive
        _emit(
            args,
            getattr(args, "command", "xmax"),
            {
                "errors": [
                    {
                        "code": "xmax.internal",
                        "message": str(exc),
                        "stage": "general",
                        "retryable": False,
                    }
                ]
            },
            ok=False,
        )
        return EXIT_INTERNAL
    finally:
        composition.close()


def _human_import(composition: Composition, args: argparse.Namespace) -> int:
    from .feedback.importer import HumanSignalImporter

    importer = HumanSignalImporter(composition.database, clock=composition.clock)
    data = importer.import_file(composition.root / args.input)
    _emit(args, "human.import", data, ok=not data["errors"])
    return 0 if not data["errors"] else EXIT_PARTIAL


def _human_action(composition: Composition, args: argparse.Namespace) -> int:
    if args.action in {"import", "feedback"}:
        return _human_import(composition, args)

    if args.action == "import-media":
        from .feedback.media_importer import HumanMediaImporter

        path = composition.root / args.input
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload if isinstance(payload, list) else payload.get("signals", [])
        importer = HumanMediaImporter(
            composition.database, composition.artifacts, clock=composition.clock
        )
        data = importer.import_records(records, source_file=str(path))
        _emit(args, "human.import-media", data, ok=not data["errors"])
        return 0 if not data["errors"] else EXIT_PARTIAL

    if args.action == "import-feishu":
        from .feedback.feishu_importer import FeishuHumanDatasetImporter
        from .feedback.media_importer import HumanMediaImporter

        config = load_config(
            composition.root / args.config,
            "human-feedback-source.schema.json",
            base_dir=composition.root,
        )
        media_importer = HumanMediaImporter(
            composition.database, composition.artifacts, clock=composition.clock
        )
        importer = FeishuHumanDatasetImporter(
            composition.feishu_client(read_only=True),
            media_importer,
            download_root=composition.root / "var" / "downloads" / "human-feedback",
        )
        data = importer.import_source(config)
        ok = not data["errors"] and not data["download_errors"]
        _emit(args, "human.import-feishu", data, ok=ok)
        return 0 if ok else EXIT_PARTIAL

    if args.action == "normalize":
        from .feedback.normalizer import MlmmHumanNormalizer, RuleNormalizer
        from .feedback.proposals import DimensionProposalService

        signals = composition.database.list_human_signals()
        requested = set(args.signal_id)
        if requested:
            signals = [item for item in signals if item.get("signal_id") in requested]
        if args.pending:
            signals = [
                item
                for item in signals
                if not item.get("normalizer_id")
                or item.get("mapping_status") == "needs_clarification"
            ]
        if args.rule_based:
            normalizer = RuleNormalizer(composition.benchmark)
        else:
            human_config = composition.project.get("human_feedback", {})
            normalizer = MlmmHumanNormalizer(
                composition.mlmm_provider(),
                threshold=float(human_config.get("confidence_threshold", 0.7)),
            )
        proposal_service = DimensionProposalService(composition.database)
        if args.workers < 1 or args.workers > 8:
            raise ConfigError("human normalize --workers must be between 1 and 8")

        normalized = []
        errors = []

        def apply_result(signal: dict[str, Any], item: dict[str, Any]) -> None:
            composition.database.update_human_signal(item)
            proposal = item.get("dimension_proposal")
            if proposal:
                proposal_id = f"proposal-{item['signal_id']}"
                proposal_service.propose(
                    {
                        **proposal,
                        "proposal_id": proposal_id,
                        "source_signal_id": item["signal_id"],
                    }
                )
            normalized.append(item["signal_id"])

        def normalize_one(
            signal: dict[str, Any],
        ) -> tuple[dict[str, Any], dict[str, Any]]:
            return signal, normalizer.normalize(signal, composition.benchmark)

        if args.workers == 1:
            for signal in signals:
                try:
                    _, item = normalize_one(signal)
                    apply_result(signal, item)
                except Exception as exc:
                    errors.append(
                        {
                            "code": "xmax.normalization_failed",
                            "message": f"{signal.get('signal_id')}: {exc}",
                            "stage": "feedback",
                            "retryable": False,
                        }
                    )
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                pending = {executor.submit(normalize_one, signal): signal for signal in signals}
                for future in as_completed(pending):
                    signal = pending[future]
                    try:
                        _, item = future.result()
                        apply_result(signal, item)
                    except Exception as exc:
                        errors.append(
                            {
                                "code": "xmax.normalization_failed",
                                "message": f"{signal.get('signal_id')}: {exc}",
                                "stage": "feedback",
                                "retryable": False,
                            }
                        )

        data = {
            "normalized": len(normalized),
            "signal_ids": normalized,
            "errors": errors,
        }
        _emit(args, "human.normalize", data, ok=not errors)
        return 0 if not errors else EXIT_PARTIAL

    if args.action == "apply-overrides":
        from .feedback.overrides import HumanOverrideService

        requested = set(args.signal_id)
        signals = composition.database.list_human_signals()
        if requested:
            signals = [item for item in signals if item.get("signal_id") in requested]
        service = HumanOverrideService(composition.database)
        applied = []
        errors = []
        for signal in signals:
            try:
                applied.append(service.apply_signal(signal))
            except Exception as exc:
                errors.append(
                    {
                        "code": "xmax.human_override_failed",
                        "message": f"{signal.get('signal_id')}: {exc}",
                        "stage": "feedback",
                        "retryable": False,
                    }
                )
        data = {"applied": len(applied), "overrides": applied, "errors": errors}
        _emit(args, "human.apply-overrides", data, ok=not errors)
        return 0 if not errors else EXIT_PARTIAL

    if args.action == "build-challenger":
        from .feedback.training import HumanLearningService

        data = HumanLearningService(composition.database).build_challenger(
            judge_id=args.judge_id,
            version=args.version,
            route_kind=args.route,
            train_path=composition.root / args.train,
            output_directory=composition.root / args.output_directory,
            trainer_entrypoint=args.trainer_entrypoint,
            base_version=args.base_version,
        )
        _emit(args, "human.build-challenger", data)
        return 0

    from .feedback.router import LearningRouter

    router = LearningRouter(composition.database, composition.benchmark)
    candidates = []
    packets_by_partition = {"train": [], "calibration": [], "holdout": []}
    excluded = []
    for signal in composition.database.list_human_signals():
        try:
            partition = None if args.repartition else signal.get("data_partition")
            if partition in {None, "unassigned"}:
                partition = router.assign_partition(signal)
                signal = {**signal, "data_partition": partition}
                composition.database.update_human_signal(signal)
            packets = router.export_packets(signal, partition=partition)
            if packets:
                if partition in packets_by_partition:
                    packets_by_partition[partition].extend(packets)
                if partition != "holdout":
                    candidates.extend(packets)
            else:
                excluded.append(signal.get("signal_id"))
        except Exception as exc:
            excluded.append(f"{signal.get('signal_id')}: {exc}")
    output = composition.root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in candidates),
        encoding="utf-8",
    )
    partition_outputs = {}
    suffix = output.suffix or ".jsonl"
    for partition, packets in packets_by_partition.items():
        partition_path = output.with_name(f"{output.stem}.{partition}{suffix}")
        partition_path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in packets
            ),
            encoding="utf-8",
        )
        partition_outputs[partition] = str(partition_path)
    data = {
        "exported": len(candidates),
        "partition_counts": {key: len(value) for key, value in packets_by_partition.items()},
        "excluded": excluded,
        "output": str(output),
        "partition_outputs": partition_outputs,
        "routes": sorted({item["route_kind"] for item in candidates}),
    }
    _emit(args, "human.partition", data)
    return 0


def _replay_run(composition: Composition, args: argparse.Namespace) -> int:
    from .evaluation.replay import ReplayService

    runs = composition.database.list_runs(run_batch_id=args.run_batch_id)
    service = ReplayService(
        composition.database,
        composition.evaluation_orchestrator(),
        composition.artifacts,
        clock=composition.clock,
    )
    data = service.replay_runs(runs)
    _emit(args, "replay.run", data)
    return 0


def _release_action(composition: Composition, args: argparse.Namespace) -> int:
    from .evaluation.release import ReleaseService

    schema = composition.score_schema()
    service = ReleaseService(composition.database, composition.benchmark, schema)
    if args.action == "validate":
        payload = json.loads((composition.root / args.holdout).read_text(encoding="utf-8"))
        holdout_results = payload if isinstance(payload, list) else payload.get("results", [])
        data = service.validate(holdout_results=holdout_results, threshold=args.threshold)
    elif args.action == "promote":
        validation = json.loads((composition.root / args.validation).read_text(encoding="utf-8"))
        data = service.promote(validation, operator=args.operator)
    else:
        data = service.rollback(args.previous_version, operator=args.operator)
    _emit(args, f"release.{args.action}", data)
    return 0


_HANDLERS: dict[str, Callable[[Composition, argparse.Namespace], int]] = {
    "project-check": cmd_project_check,
    "benchmark-check": cmd_benchmark_check,
    "db": lambda c, a: cmd_db_migrate(c, a) if a.action == "migrate" else cmd_db_check(c, a),
    "artifacts": lambda c, a: (
        cmd_artifacts_verify(c, a) if a.action == "verify" else cmd_artifacts_gc(c, a)
    ),
    "context-check": cmd_context_check,
    "run": cmd_run,
    "ingest": lambda c, a: (
        cmd_ingest_assets(c, a) if a.action == "assets" else cmd_ingest_results(c, a)
    ),
    "plan": cmd_plan,
    "task": cmd_task,
    "worker": cmd_worker,
    "generate": lambda c, a: (
        cmd_generate_offline(c, a) if a.action == "offline" else cmd_generate_realtime(c, a)
    ),
    "preprocess": cmd_preprocess,
    "evaluate": cmd_evaluate,
    "evaluation-budget": cmd_evaluation_budget,
    "report": cmd_report,
    "sync": cmd_sync,
    "reconcile": cmd_reconcile,
    "stage": cmd_stage_show,
    "batch": cmd_batch_show,
    "judges": cmd_judges,
    "human": _human_action,
    "replay": _replay_run,
    "release": _release_action,
}
