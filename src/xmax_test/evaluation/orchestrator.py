"""Evaluation orchestrator.

Loads the current Benchmark and Scenario Pack, routes dimensions to judges,
consumes existing preprocess evidence, validates judgments, applies hard gates and
fusion, and persists evaluations plus the evaluation batch. It never calls the
generation adapter and never downloads from Feishu.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..errors import (
    ContractError,
    EvaluationBudgetPausedError,
    EvaluationInfrastructurePausedError,
)
from ..hashing import content_hash
from ..time import utc_now
from .aggregation import aggregate_evaluation_results
from .fusion import JudgmentFusion
from .group_metrics import BatchGroupEvaluator
from .preprocess import PreprocessService


class EvaluationOrchestrator:
    def __init__(
        self,
        repository: Any,
        artifacts: Any,
        benchmark: dict[str, Any],
        scenario_pack: dict[str, Any],
        judge_registry: Any,
        worker: Any,
        preprocess: PreprocessService,
        fusion: JudgmentFusion | None = None,
        recipe_resolver: Any = None,
        budget_gate: Any = None,
        clock: Any = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._benchmark = benchmark
        self._scenario_pack = scenario_pack
        self._judge_registry = judge_registry
        self._worker = worker
        self._preprocess = preprocess
        self._fusion = fusion or JudgmentFusion()
        self._recipe_resolver = recipe_resolver
        self._budget_gate = budget_gate
        self._clock = clock

    def evaluate_runs(
        self,
        runs: list[dict[str, Any]],
        preprocess_by_run: dict[str, dict[str, Any]] | None = None,
        *,
        resume: bool = False,
    ) -> dict[str, Any]:
        if not runs:
            raise ContractError("evaluate requires at least one run")
        fingerprint = content_hash(
            {
                "runs": sorted(r["run_id"] for r in runs),
                "benchmark_version": self._benchmark.get("benchmark_version"),
                "scenario_pack_version": self._scenario_pack.get("version"),
                "judges": self._judge_registry.manifests(),
            }
        )
        evaluation_batch_id = self._evaluation_batch_id(fingerprint, resume)
        results: list[dict[str, Any]] = []
        coverage: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for run in runs:
            try:
                if resume:
                    existing = self._repository.list_evaluation_results(
                        run_id=run["run_id"],
                        evaluation_batch_id=evaluation_batch_id,
                    )
                    if existing:
                        results.append(existing[-1])
                        continue
                preprocess = (preprocess_by_run or {}).get(run["run_id"])
                result = self.evaluate_run(run, evaluation_batch_id, preprocess=preprocess)
                results.append(result)
            except Exception as exc:
                if isinstance(
                    exc,
                    (EvaluationBudgetPausedError, EvaluationInfrastructurePausedError),
                ):
                    raise
                errors.append(
                    {
                        "code": getattr(exc, "code", "xmax.contract_error"),
                        "message": f"{run.get('run_id')}: {exc}",
                        "stage": "evaluate",
                        "retryable": bool(getattr(exc, "retryable", False)),
                        "entity_id": run.get("run_id"),
                    }
                )
        results = self.finalize_batch_context(runs, results)
        aggregate = aggregate_evaluation_results(results)
        if results:
            manifest = {
                "manifest_version": "1.0",
                "batch_id": evaluation_batch_id,
                "entity_type": "evaluation_batch",
                "item_entity_type": "evaluation_result",
                "item_ids": sorted(r["evaluation_id"] for r in results),
                "content_hash": content_hash(
                    {"evaluation_ids": sorted(r["evaluation_id"] for r in results)}
                ),
                "producer_stage_run_id": f"evaluate-{evaluation_batch_id[-8:]}",
                "created_at": utc_now(),
                "metadata": {
                    "benchmark_version": self._benchmark.get("benchmark_version"),
                    "scenario_pack_version": self._scenario_pack.get("version"),
                    "score_source": "criterion_results",
                    "evaluation_fingerprint": fingerprint,
                    "aggregate": aggregate,
                },
            }
            self._repository.save_batch_manifest(manifest)
        return {
            "evaluation_batch_id": evaluation_batch_id,
            "evaluated": len(results),
            "failed": len(errors),
            "errors": errors,
            "coverage": coverage,
            "results": results,
            "aggregate": aggregate,
            "criterion_summary": aggregate["criterion_summary"],
            "dimension_summary": aggregate["dimension_summary"],
            "case_score_summary": aggregate["case_score_summary"],
        }

    def _evaluation_batch_id(self, fingerprint: str, resume: bool) -> str:
        if resume:
            matches = [
                item
                for item in self._repository.list_batch_manifests("evaluation_batch")
                if item.get("metadata", {}).get("evaluation_fingerprint") == fingerprint
            ]
            if matches:
                return matches[-1]["batch_id"]
        return f"eval-{fingerprint[:12]}-{uuid.uuid4().hex[:8]}"

    def finalize_batch_context(
        self,
        runs: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Add batch-only criteria and deterministically re-fuse each result."""

        by_run = {item["run_id"]: item for item in results}
        generated = BatchGroupEvaluator(self._repository).judgments(runs, results)
        for run_id, extra_judgments in generated.items():
            previous = by_run.get(run_id)
            run = next((item for item in runs if item.get("run_id") == run_id), None)
            if previous is None or run is None:
                continue
            for judgment in extra_judgments:
                self._repository.append_judgment(judgment)
            case = self._repository.get_test_case(run["case_id"])
            fused = self._fusion.fuse(
                self._benchmark,
                self._scenario_pack,
                case,
                self._repository.list_judgments(previous["evaluation_id"]),
                run.get("metrics", {}),
            )
            updated = {
                **previous,
                **fused,
                "evaluation_id": previous["evaluation_id"],
                "evaluation_batch_id": previous["evaluation_batch_id"],
                "run_id": run_id,
            }
            self._repository.save_evaluation_result(updated)
            by_run[run_id] = updated
        return [by_run[item["run_id"]] for item in results]

    def evaluate_run(
        self,
        run: dict[str, Any],
        evaluation_batch_id: str,
        *,
        preprocess: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Once the paid budget closes, no CV, metric or MLLM Judge may begin.
        if self._budget_gate is not None:
            self._budget_gate.assert_evaluation_allowed()
        if run.get("status") != "completed":
            raise ContractError(f"evaluate requires completed run, got {run.get('status')}")
        if not run.get("result_asset_id"):
            raise ContractError(f"run {run['run_id']} has no result asset")
        case = self._repository.get_test_case(run["case_id"])
        mode = run.get("mode", "offline")

        applicable = self._applicable_dimensions(mode)
        preprocess = preprocess or self._repository.get_preprocess_for_run(run["run_id"])
        if preprocess.get("run_id") != run["run_id"] or preprocess.get("status") != "completed":
            raise ContractError(
                f"preprocess {preprocess.get('preprocess_id')} does not provide completed evidence "
                f"for run {run['run_id']}"
            )

        evaluation_id = f"eval-{uuid.uuid4().hex[:16]}"
        judgments: list[dict[str, Any]] = []
        evidence_groups = self._evidence_groups(preprocess)
        operation_contract = self._operation_contract(case, mode)
        evidence_images = [image for group in evidence_groups for image in group["images"]]
        common_context = {
            "evaluation_id": evaluation_id,
            "run_id": run["run_id"],
            "benchmark_version": self._benchmark.get("benchmark_version", ""),
            "mode": mode,
            "test_case": case,
            "run": run,
            "preprocess": preprocess,
            "media": self._media_of(run),
            "asset_paths": self._asset_paths(run),
            "user_prompt": case.get("prompt_text", ""),
            "evidence_images": evidence_images,
            "evidence_groups": evidence_groups,
            "media_inputs": self._media_inputs(
                case,
                run,
                operation_contract,
                preprocess=preprocess,
            ),
            "operation_contract": operation_contract,
            "output_schema": self._judgment_schema(),
        }
        selected_by_dimension: dict[str, list[tuple[str, str]]] = {}
        batched: dict[tuple[str, str], list[str]] = {}
        for dimension_id in applicable:
            dimension = self._dimension(dimension_id)
            routing = dimension.get("judge_routing", {})
            judge_items = self._resolve_judges(dimension_id, mode)
            if not judge_items and routing.get("fallback_policy") == "no_automated_judge":
                judgments.append(
                    {
                        "evaluation_id": evaluation_id,
                        "run_id": run["run_id"],
                        "benchmark_version": self._benchmark.get("benchmark_version", ""),
                        "dimension_id": dimension_id,
                        "dimension_version": dimension.get("version", ""),
                        "judge_id": "none",
                        "judge_version": "none",
                        "verdict": "no_automated_judge",
                        "status": "no_automated_judge",
                        "evidence": [],
                    }
                )
                continue
            selected_by_dimension[dimension_id] = []
            for judge_item in judge_items:
                registered = self._judge_registry.get(*judge_item)
                if registered.manifest.get("batch_dimensions"):
                    batched.setdefault(judge_item, []).append(dimension_id)
                else:
                    selected_by_dimension[dimension_id].append(judge_item)

        for dimension_id, judge_items in selected_by_dimension.items():
            if not judge_items:
                continue
            dimension = self._dimension(dimension_id)
            for judge_item in judge_items:
                registered = self._judge_registry.get(*judge_item)
                judge_dimension = self._contract_for_judge(dimension, registered)
                if not judge_dimension.get("criteria"):
                    continue
                judgments.extend(
                    self._worker.run(
                        evaluation_id=evaluation_id,
                        run_id=run["run_id"],
                        benchmark_version=self._benchmark.get("benchmark_version", ""),
                        dimension_id=dimension_id,
                        dimension_version=dimension.get("version", ""),
                        mode=mode,
                        context={
                            **common_context,
                            "dimension_contract": judge_dimension,
                            "prompt": self._judge_prompt(
                                judge_dimension,
                                case,
                                mode,
                                evidence_groups,
                                operation_contract,
                            ),
                        },
                        judge_ids=[judge_item],
                    )
                )

        for (judge_id, judge_version), dimension_ids in batched.items():
            registered = self._judge_registry.get(judge_id, judge_version)
            dimensions = [
                self._contract_for_judge(self._dimension(item), registered)
                for item in dimension_ids
            ]
            dimensions = [item for item in dimensions if item.get("criteria")]
            if not dimensions:
                continue
            dimension_versions = {
                item["dimension_id"]: item.get("version", "") for item in dimensions
            }
            judgments.extend(
                self._worker.run_batch(
                    evaluation_id=evaluation_id,
                    run_id=run["run_id"],
                    benchmark_version=self._benchmark.get("benchmark_version", ""),
                    mode=mode,
                    context={
                        **common_context,
                        "dimension_contracts": dimensions,
                        "prompt": self._mlmm_prompt(
                            dimensions, case, mode, evidence_groups, operation_contract
                        ),
                    },
                    judge_id=judge_id,
                    judge_version=judge_version,
                    dimension_versions=dimension_versions,
                )
            )

        for judgment in judgments:
            if judgment.get("status") == "no_automated_judge":
                continue
            self._repository.append_judgment(judgment)

        result = self._fusion.fuse(
            self._benchmark,
            self._scenario_pack,
            case,
            judgments,
            run.get("metrics", {}),
        )
        result["evaluation_id"] = evaluation_id
        result["evaluation_batch_id"] = evaluation_batch_id
        result["run_id"] = run["run_id"]
        result["model_id"] = run.get("model_id")
        result["generation_config_hash"] = content_hash(case.get("generation_config", {}))
        result["preprocess_id"] = preprocess.get("preprocess_id")
        result["preprocessor_version"] = preprocess.get("producer_version")
        self._repository.save_evaluation_result(result)
        return result

    def _applicable_dimensions(self, mode: str) -> list[str]:
        result = []
        for dimension in self._benchmark.get("dimensions", []):
            modes = dimension.get("applicable_modes", [])
            if mode in modes or "both" in modes:
                result.append(dimension["dimension_id"])
        return result

    def _dimension(self, dimension_id: str) -> dict[str, Any]:
        for dimension in self._benchmark.get("dimensions", []):
            if dimension.get("dimension_id") == dimension_id:
                return dimension
        raise ContractError(f"unknown dimension {dimension_id}")

    def _resolve_judges(self, dimension_id: str, mode: str) -> list[tuple[str, str]]:
        matches = self._judge_registry.for_dimension(dimension_id, mode)
        routing = self._dimension(dimension_id).get("judge_routing", {})
        primary_kinds = set(routing.get("primary_kinds", []))
        secondary_kinds = set(routing.get("secondary_kinds", []))
        primary = [
            item
            for item in matches
            if item.manifest.get("kind") in primary_kinds
            and self._contract_for_judge(self._dimension(dimension_id), item).get("criteria")
        ]
        secondary = [
            item
            for item in matches
            if item.manifest.get("kind") in secondary_kinds
            and self._contract_for_judge(self._dimension(dimension_id), item).get("criteria")
        ]
        # Primary and secondary judges are complementary evidence producers.
        # Keeping both also permits an assessable secondary result when a
        # primary CV backend truthfully returns unassessable for one sample.
        selected = list(
            {(item.judge_id, item.version): item for item in primary + secondary}.values()
        )
        return [(item.judge_id, item.version) for item in selected]

    @staticmethod
    def _contract_for_judge(dimension: dict[str, Any], registered: Any) -> dict[str, Any]:
        """Return only criteria explicitly owned by this Judge.

        A legacy manifest without ``supported_criteria`` is allowed for test
        plugins, but production config validation reports it as incomplete.
        """

        supported = set(registered.manifest.get("supported_criteria") or [])
        if not supported:
            return dict(dimension)
        return {
            **dimension,
            "criteria": [
                criterion
                for criterion in dimension.get("criteria", [])
                if criterion.get("criterion_id") in supported
            ],
        }

    def _media_of(self, run: dict[str, Any]) -> dict[str, Any]:
        result_asset_id = run.get("result_asset_id")
        if not result_asset_id:
            return {}
        asset = self._repository.get_asset(result_asset_id)
        return asset.get("media", {})

    def _asset_paths(self, run: dict[str, Any]) -> dict[str, str]:
        paths: dict[str, str] = {}
        for key, asset_id in (
            ("result_video", run.get("result_asset_id")),
            ("expected_audio_source", run.get("expected_audio_source_asset_id")),
            ("edited_video", run.get("edited_video_asset_id")),
        ):
            if not asset_id:
                continue
            try:
                asset = self._repository.get_asset(asset_id)
                uri = asset.get("uri", "")
                if uri.startswith("artifact://"):
                    path = self._artifacts.resolve(uri)
                    if path.is_file():
                        paths[key] = str(path.resolve())
            except Exception:
                continue
        return paths

    def _media_inputs(
        self,
        case: dict[str, Any],
        run: dict[str, Any],
        operation_contract: dict[str, Any] | None = None,
        *,
        preprocess: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve original, role-labelled media for direct-video MLLM providers.

        The order is a contract: Feed, literal Prompt text, Prompt references,
        then generated Result. Providers that cannot accept video keep using the
        separately supplied evidence_images fallback.
        """

        result: list[dict[str, Any]] = []
        if operation_contract:
            result.append(
                {
                    "role": "generation_operation",
                    "kind": "text",
                    "text": json.dumps(
                        operation_contract,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
        media_urls = self._run_media_urls(case, run)
        if case.get("feed_asset_id"):
            asset_id = case["feed_asset_id"]
            item = self._media_input("feed", asset_id, media_urls.get(asset_id))
            if item:
                result.append(item)
        feed_capture = self._feed_capture_input(case)
        if feed_capture:
            result.append(feed_capture)
        result.append(
            {
                "role": "prompt_text",
                "kind": "text",
                "text": case.get("prompt_text", ""),
            }
        )
        for index, asset_id in enumerate(case.get("prompt_asset_ids", []), start=1):
            item = self._media_input(
                f"prompt_reference_{index}", asset_id, media_urls.get(asset_id)
            )
            if item:
                result.append(item)
        if run.get("result_asset_id"):
            asset_id = run["result_asset_id"]
            provider_media = (preprocess or {}).get("provider_media", {}).get("result_video")
            item = (
                self._provider_media_input("result_video", provider_media, asset_id)
                if provider_media
                else self._media_input("result_video", asset_id, media_urls.get(asset_id))
            )
            if item:
                result.append(item)
        return result

    def _provider_media_input(
        self,
        role: str,
        provider_media: dict[str, Any],
        source_asset_id: str,
    ) -> dict[str, Any] | None:
        try:
            uri = str(provider_media.get("uri") or "")
            path = self._artifacts.resolve(uri)
            if not path.is_file():
                return None
            return {
                "role": role,
                "kind": "video",
                "path": str(path.resolve()),
                "asset_id": source_asset_id,
                "derived_from_asset_id": source_asset_id,
                "normalizer_version": provider_media.get("normalizer_version"),
            }
        except Exception:
            return None

    def _operation_contract(self, case: dict[str, Any], mode: str) -> dict[str, Any]:
        evaluation = dict(case.get("evaluation_operation_contract") or {})
        recipe: dict[str, Any] = {}
        if self._recipe_resolver is not None and case.get("operation_recipe_id"):
            try:
                recipe = self._recipe_resolver.recipe(case["operation_recipe_id"])
            except Exception:
                recipe = {}
        if not evaluation:
            evaluation = dict(recipe.get("evaluation_contract") or {})
        return {
            "contract_kind": "generation_operation",
            "recipe_id": case.get("operation_recipe_id"),
            "recipe_version": case.get("operation_recipe_version"),
            "generation_mode": mode,
            "operation_summary": evaluation.get("operation_summary", ""),
            "result_expectation": evaluation.get("result_expectation", ""),
            "role_semantics": evaluation.get("role_semantics", {}),
            "must_preserve": evaluation.get("must_preserve", []),
            "must_change": evaluation.get("must_change", []),
            "edited_video_role": recipe.get("edited_video_role"),
            "expected_audio_source_role": recipe.get("expected_audio_source_role"),
            "api_asset_bindings": case.get("api_asset_bindings", {}),
            "resolved_asset_roles": {
                "feed_asset_id": case.get("feed_asset_id"),
                "prompt_asset_ids": case.get("prompt_asset_ids", []),
                "edited_video_asset_id": case.get("edited_video_asset_id"),
                "expected_audio_source_asset_id": case.get("expected_audio_source_asset_id"),
            },
        }

    def _feed_capture_input(self, case: dict[str, Any]) -> dict[str, Any] | None:
        if case.get("api_asset_bindings", {}).get("refImagePath") != "feed_capture":
            return None
        try:
            feed = self._repository.get_asset(case["feed_asset_id"])
            capture_id = f"{feed['asset_id']}-{str(feed.get('sha256') or '')[:12]}"
            uri = f"artifact://captures/{capture_id}/middle.jpg"
            path = self._artifacts.resolve(uri)
            if not path.is_file():
                return None
            return {
                "role": "feed_capture",
                "kind": "image",
                "path": str(path.resolve()),
                "source_asset_id": feed["asset_id"],
            }
        except Exception:
            return None

    def _media_input(
        self, role: str, asset_id: str, url: str | None = None
    ) -> dict[str, Any] | None:
        try:
            asset = self._repository.get_asset(asset_id)
            uri = asset.get("uri", "")
            if not uri.startswith("artifact://"):
                return None
            path = self._artifacts.resolve(uri)
            if not path.is_file():
                return None
            kind = str(asset.get("kind") or "")
            mime = str(asset.get("mime_type") or "")
            media_kind = (
                "video" if kind.endswith("_video") or mime.startswith("video/") else "image"
            )
            item = {
                "role": role,
                "kind": media_kind,
                "path": str(path.resolve()),
                "asset_id": asset_id,
            }
            if url:
                item["url"] = url
            return item
        except Exception:
            return None

    def _run_media_urls(self, case: dict[str, Any], run: dict[str, Any]) -> dict[str, str]:
        """Recover already-published XMAX URLs without re-uploading media.

        Input uploads include the content SHA in their object names. Result URLs
        are recorded in run metrics. Imported/local runs without such provenance
        correctly fall back to Base64 and its explicit per-item limit.
        """

        urls: list[str] = []
        metrics = run.get("metrics") or {}
        result_url = metrics.get("result_url")
        if isinstance(result_url, str) and result_url.startswith("https://"):
            result_asset_id = run.get("result_asset_id")
            if result_asset_id:
                return_urls = {result_asset_id: result_url}
            else:
                return_urls = {}
        else:
            return_urls = {}
        try:
            events = self._repository.get_event_log(run["run_id"])
        except Exception:
            events = []
        for event in events:
            urls.extend(self._https_urls(event.get("payload")))
        asset_ids = [case.get("feed_asset_id"), *case.get("prompt_asset_ids", [])]
        for asset_id in [item for item in asset_ids if item]:
            try:
                asset = self._repository.get_asset(asset_id)
                sha = str(asset.get("sha256") or "")
            except Exception:
                continue
            if not sha:
                continue
            match = next((url for url in urls if sha in url), None)
            if match:
                return_urls[asset_id] = match
        return return_urls

    @classmethod
    def _https_urls(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value] if value.startswith("https://") else []
        if isinstance(value, dict):
            return [url for item in value.values() for url in cls._https_urls(item)]
        if isinstance(value, list):
            return [url for item in value for url in cls._https_urls(item)]
        return []

    def _evidence_groups(self, preprocess: dict[str, Any]) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        source_groups = preprocess.get("evidence_groups") or [
            {"role": sheet.get("kind", "unknown"), "frames": sheet.get("frames", [])}
            for sheet in preprocess.get("sheets", [])
        ]
        for group in source_groups:
            paths: list[str] = []
            for frame in group.get("frames", []):
                uri = frame.get("uri")
                if isinstance(uri, str) and uri.startswith("artifact://"):
                    path = self._artifacts.resolve(uri)
                    if path.is_file():
                        paths.append(str(path.resolve()))
            if paths:
                groups.append(
                    {
                        "role": group.get("role", "unknown"),
                        "asset_id": group.get("asset_id"),
                        "images": list(dict.fromkeys(paths)),
                    }
                )
        return groups

    def _judge_prompt(
        self,
        dimension: dict[str, Any],
        case: dict[str, Any],
        mode: str,
        evidence_groups: list[dict[str, Any]],
        operation_contract: dict[str, Any],
    ) -> str:
        criteria = [
            {
                "criterion_id": item.get("criterion_id"),
                "name": item.get("name"),
                "definition": item.get("definition"),
                "anchors": item.get("anchors", {}),
            }
            for item in dimension.get("criteria", [])
        ]
        payload = {
            "benchmark_version": self._benchmark.get("benchmark_version"),
            "dimension_id": dimension.get("dimension_id"),
            "dimension_version": dimension.get("version"),
            "dimension_name": dimension.get("name"),
            "definition": dimension.get("definition"),
            "criteria": criteria,
            "mode": mode,
            "user_prompt": case.get("prompt_text", ""),
            "generation_operation": operation_contract,
            "evidence_order": [
                {"role": group["role"], "image_count": len(group["images"])}
                for group in evidence_groups
            ],
            "evidence_image_count": sum(len(group["images"]) for group in evidence_groups),
        }
        return (
            "你是盲评视频质量评测器。不得猜测模型名称、版本或未在证据中出现的事实。"
            "只评价下面一个维度；抽帧无法证明的连续性、延迟、音频或因果关系必须标记不可评。"
            "按0差、1合格、2好的尺度，对合同中每一条criterion独立评分。"
            "输出一个JSON对象，不要Markdown。字段必须包含："
            "verdict,confidence,assessable,evidence,criterion_results。"
            "criterion_results必须恰好包含合同中所有criterion_id；每项必须包含"
            "criterion_id,verdict,score,confidence,assessable,evidence。"
            "不可评时score为null并说明原因。不要输出维度score，系统只会从细则分汇总维度。"
            "系统会补齐评测、Run、维度和Judge身份字段。"
            "evidence必须引用可见时刻或说明不可评原因。\n评测合同："
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )

    def _mlmm_prompt(
        self,
        dimensions: list[dict[str, Any]],
        case: dict[str, Any],
        mode: str,
        evidence_groups: list[dict[str, Any]],
        operation_contract: dict[str, Any],
    ) -> str:
        contracts = []
        for dimension in dimensions:
            contracts.append(
                {
                    "dimension_id": dimension.get("dimension_id"),
                    "dimension_version": dimension.get("version"),
                    "dimension_name": dimension.get("name"),
                    "definition": dimension.get("definition"),
                    "criteria": [
                        {
                            "criterion_id": criterion.get("criterion_id"),
                            "name": criterion.get("name"),
                            "definition": criterion.get("definition"),
                            "anchors": criterion.get("anchors", {}),
                        }
                        for criterion in dimension.get("criteria", [])
                    ],
                }
            )
        payload = {
            "benchmark_version": self._benchmark.get("benchmark_version"),
            "dimensions": contracts,
            "mode": mode,
            "user_prompt": case.get("prompt_text", ""),
            "generation_operation": operation_contract,
            "evidence_order": [
                {"role": group["role"], "image_count": len(group["images"])}
                for group in evidence_groups
            ],
        }
        return (
            "你是盲评视频质量评测器。不得猜测模型名称、版本、历史人工结论或未在证据中出现的事实。"
            "必须先按generation_operation理解本玩法中Feed、Feed截图、Prompt素材和被编辑视频的真实职责，"
            "再评价结果；不得仅按文件名或通常含义猜素材角色。"
            "一次评价下面列出的全部维度，每个维度恰好输出一项。若输入包含原始视频，"
            "可以依据其可见画面判断连续性和时序；若只有抽帧则不得推断连续性。"
            "任何视觉输入都不能证明音频、API延迟或未显示的运行因果，这些必须标记不可评。"
            "按0差、1合格、2好的尺度返回JSON对象，顶层字段为judgments；每个维度项必须包含"
            "dimension_id,verdict,confidence,assessable,evidence,criterion_results。"
            "criterion_results必须恰好包含该维度合同中的全部criterion_id，且每条细则必须包含"
            "criterion_id,verdict,score,confidence,assessable,evidence。不可评时score为null。"
            "不要输出维度score；系统只会从细则分确定性汇总维度和总分。不要Markdown。"
            "不同维度不可复制同一理由代替独立判断。系统会补齐评测、Run、版本和Judge身份。\n评测合同："
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )

    @staticmethod
    def _judgment_schema() -> dict[str, Any]:
        schema_path = Path(__file__).resolve().parents[3] / "schemas" / "judgment.schema.json"
        return json.loads(schema_path.read_text(encoding="utf-8"))
