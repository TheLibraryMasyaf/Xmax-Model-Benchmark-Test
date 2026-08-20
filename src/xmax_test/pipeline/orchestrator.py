"""Pipeline orchestrator.

Parses a Run Request, freezes selectors, checks explicit dependencies, calls
the authorized stages in topological order and writes Stage Manifests. It
never modifies business artifacts and never silently expands ``stages``.
"""

from __future__ import annotations

from typing import Any

from ..contracts import EntityRef, PipelineStage
from ..errors import (
    ApprovalRequiredError,
    ContractError,
    MissingInputError,
    PartialCompletionError,
)
from ..hashing import content_hash
from ..time import Clock, SystemClock
from .dependencies import check_stage_inputs, raise_missing_inputs
from .manifests import MANIFEST_VERSION, PRODUCER_VERSION, ManifestStore, build_stage_manifest, ref_for_batch
from .models import (
    StageExecutionRequest,
    StageExecutionResult,
    output_entity_type,
    topological_sort,
)
from .selectors import SelectorResolver, frozen_refs


class PipelineOrchestrator:
    def __init__(
        self,
        repository: Any,
        manifest_store: ManifestStore,
        selectors: SelectorResolver,
        executors: dict[PipelineStage, Any],
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._manifest_store = manifest_store
        self._selectors = selectors
        self._executors = executors
        self._clock = clock or SystemClock()

    def run(
        self,
        request: dict[str, Any],
        *,
        dry_run: bool = False,
        resume: bool = False,
        smoke_limit: int | None = None,
        budget_approved: bool = False,
    ) -> dict[str, Any]:
        stages = list(request.get("stages", []))
        if not stages:
            raise ContractError("run request requires at least one stage")
        if len(stages) != len(set(stages)):
            raise ContractError("run request stages must be unique")

        ordered = topological_sort(stages)
        frozen = self._freeze_selectors(request)
        available: dict[str, list[EntityRef]] = {}
        executed: list[dict[str, Any]] = []
        all_errors: list[dict[str, Any]] = []

        for stage in ordered:
            stage_input_refs = self._stage_input_refs(stage, frozen, available)
            config_snapshot = self._stage_config(request, stage, dry_run, smoke_limit, budget_approved)
            if "generate" == stage and not dry_run and not budget_approved:
                raise ApprovalRequiredError(
                    "generate stage requires an approved budget before a real run; "
                    "use --dry-run to preview or bind a budget approval"
                )
            # Selector-provided inputs satisfy the stage contract without
            # running any upstream stage.
            provided_view = dict(available)
            for ref in stage_input_refs:
                provided_view.setdefault(ref.entity_type, []).append(ref)
            missing = check_stage_inputs(stage, provided_view, set(stages))
            if missing:
                for item in missing:
                    all_errors.append(item)
                continue

            input_hash = content_hash(
                [(ref.entity_type, ref.entity_id, ref.content_hash) for ref in stage_input_refs]
            )
            config_hash = content_hash(config_snapshot)

            existing = self._repository.find_stage_run(
                stage, input_hash, config_hash, PRODUCER_VERSION
            )
            if existing is not None and not resume:
                executed.append(self._describe_skipped(stage, existing))
                self._publish_outputs(existing, available)
                continue

            result, manifest = self._execute_stage(
                stage,
                stage_input_refs,
                config_snapshot,
                input_hash,
                config_hash,
                dry_run=dry_run,
                smoke_limit=smoke_limit,
                resume=resume,
            )
            executed.append(self._describe(stage, manifest))
            if result.errors:
                all_errors.extend(result.errors)
            if result.status in {"completed", "partial"}:
                self._publish_outputs(manifest, available)

        summary = {
            "request_id": request.get("request_id"),
            "stages": stages,
            "executed": executed,
            "errors": all_errors,
            "ok": not all_errors and not any(item["status"] == "error" for item in executed),
        }
        if all_errors:
            raise PartialCompletionError(
                f"{len(all_errors)} stage error(s); see errors for details",
                errors=all_errors,
            )
        return summary

    # ------------------------------------------------------------------
    def _freeze_selectors(self, request: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        frozen: dict[str, list[dict[str, Any]]] = {}
        for stage, selectors in (request.get("stage_inputs") or {}).items():
            frozen[stage] = [self._selectors.resolve(item) for item in selectors]
        return frozen

    def _stage_input_refs(
        self,
        stage: str,
        frozen: dict[str, list[dict[str, Any]]],
        available: dict[str, list[EntityRef]],
    ) -> list[EntityRef]:
        refs: list[EntityRef] = []
        for selector in frozen.get(stage, []):
            refs.extend(frozen_refs(selector))
        for entity_type, items in available.items():
            if entity_type in self._consumes(stage):
                refs.extend(items)
        return refs

    @staticmethod
    def _consumes(stage: str) -> set[str]:
        from .models import STAGE_REQUIRED_INPUTS

        return set(STAGE_REQUIRED_INPUTS.get(stage, ()))

    def _stage_config(
        self,
        request: dict[str, Any],
        stage: str,
        dry_run: bool,
        smoke_limit: int | None,
        budget_approved: bool,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "stage": stage,
            "sync_policy": request.get("sync_policy"),
            "dry_run": dry_run,
            "smoke_limit": smoke_limit,
            "budget_approved": budget_approved,
            "manifest_version": MANIFEST_VERSION,
        }
        if stage == "generate":
            snapshot.update(
                {
                    "generation_modes": request.get("generation_modes"),
                    "repeat_count": request.get("repeat_count"),
                    "generation_mode_overrides": request.get("generation_mode_overrides"),
                    "execution_mode": request.get("execution_mode", "streaming"),
                    "pipeline_queue_size": request.get("pipeline_queue_size", 4),
                }
            )
        if stage == "plan":
            snapshot.update(
                {
                    "generation_modes": request.get("generation_modes"),
                    "repeat_count": request.get("repeat_count"),
                    "generation_mode_overrides": request.get(
                        "generation_mode_overrides"
                    ),
                    "generation_config": request.get("generation_config", {}),
                    "filters": request.get("filters", {}),
                    "seed": request.get("seed"),
                    "benchmark_path": request.get("benchmark_path"),
                    "scenario_pack_path": request.get("scenario_pack_path"),
                }
            )
        if stage in {"preprocess", "evaluate"}:
            snapshot.update(
                {
                    "execution_mode": request.get("execution_mode", "streaming"),
                    "pipeline_queue_size": request.get("pipeline_queue_size", 4),
                }
            )
        if stage == "report":
            snapshot["comparison"] = request.get("comparison")
        return snapshot

    def _execute_stage(
        self,
        stage: str,
        input_refs: list[EntityRef],
        config_snapshot: dict[str, Any],
        input_hash: str,
        config_hash: str,
        *,
        dry_run: bool,
        smoke_limit: int | None,
        resume: bool,
    ) -> tuple[StageExecutionResult, dict[str, Any]]:
        try:
            executor = self._executors[PipelineStage(stage)]
        except KeyError as exc:
            raise MissingInputError(
                f"no executor registered for stage {stage}",
                entity_id=stage,
                suggested_command=f"xmax-test stage show --stage-run-id <id>",
            ) from exc

        stage_run_id = f"stage-{stage}-{config_hash[:8]}"
        request = StageExecutionRequest(
            stage_run_id=stage_run_id,
            stage=PipelineStage(stage),
            input_refs=tuple(input_refs),
            config_snapshot=config_snapshot,
            dry_run=dry_run,
            smoke_limit=smoke_limit,
            resume=resume,
        )
        result = executor.execute(request)

        # Output refs are derived from batch manifests when the executor only
        # returned batches, keeping the executor contract small.
        output_refs = list(result.output_refs)
        if not output_refs:
            output_refs = [ref_for_batch(batch) for batch in result.batch_manifests]
        if dry_run and not output_refs:
            entity_type = output_entity_type(stage)
            if entity_type:
                placeholder_id = f"dry-run-{stage}-{config_hash[:8]}"
                output_refs = [
                    EntityRef(
                        entity_type=entity_type,
                        entity_id=placeholder_id,
                        content_hash=content_hash(
                            {
                                "dry_run": True,
                                "stage": stage,
                                "input_hash": input_hash,
                                "config_hash": config_hash,
                            }
                        ),
                    )
                ]

        manifest = build_stage_manifest(
            stage=stage,
            input_refs=tuple(input_refs),
            output_refs=output_refs,
            input_hash=input_hash,
            config_hash=config_hash,
            status=result.status,
            started_at=self._clock.now(),
            attempt_count=1,
            errors=result.errors,
            metadata=result.metadata,
            stage_run_id=stage_run_id,
        )
        manifest["completed_at"] = self._clock.now()
        self._manifest_store.save_stage_manifest(manifest)
        for batch in result.batch_manifests:
            batch["producer_stage_run_id"] = stage_run_id
            self._manifest_store.save_batch_manifest(batch)
        return result, manifest

    def _publish_outputs(self, manifest: dict[str, Any], available: dict[str, list[EntityRef]]) -> None:
        for ref_data in manifest.get("output_refs", []):
            ref = EntityRef(**ref_data)
            available.setdefault(ref.entity_type, []).append(ref)

    @staticmethod
    def _describe(stage: str, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "stage": stage,
            "stage_run_id": manifest["stage_run_id"],
            "status": manifest["status"],
            "input_hash": manifest["input_hash"],
            "config_hash": manifest["config_hash"],
        }

    @staticmethod
    def _describe_skipped(stage: str, existing: dict[str, Any]) -> dict[str, Any]:
        return {
            "stage": stage,
            "stage_run_id": existing.get("stage_run_id"),
            "status": "skipped",
            "input_hash": existing.get("input_hash"),
            "config_hash": existing.get("config_hash"),
        }
