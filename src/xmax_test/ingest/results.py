"""Existing-result import service.

Normalizes Feishu Case records, local directories or Stage Manifests into real
Assets, TestCases and ``status=completed`` GenerationRuns with
``origin=feishu_import/local_import/stage_manifest_import``. Importing never
writes remote; downstream evaluation needs no source special-casing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ContractError, MissingInputError
from ..hashing import content_hash
from ..planning.recipes import RecipeResolver
from ..time import utc_now
from .feishu_case import FeishuCaseImporter
from .local_results import LocalResultsImporter
from .manifest_results import ManifestResultsImporter
from .models import ImportOutcome

MODE_ORDER = ("explicit_field", "operation_recipe", "fixed_default")


class ImportService:
    def __init__(
        self,
        repository: Any,
        registry: Any,
        recipes: RecipeResolver,
        download_dir: Path,
        clock: Any = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._recipes = recipes
        self._download_dir = Path(download_dir)
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def import_results(self, config: dict[str, Any], feishu_client: Any = None) -> ImportOutcome:
        kind = config.get("source", {}).get("kind")
        snapshot = self._snapshot(config, kind)
        source_hash = content_hash(snapshot)

        prior = self._repository.get_result_import(config.get("import_request_id", ""), source_hash)
        if prior is not None:
            outcome = ImportOutcome(
                import_request_id=config.get("import_request_id", ""),
                source_hash=source_hash,
                status="skipped",
                skipped=prior.get("imported", 0),
                run_batch_id=prior.get("run_batch_id"),
            )
            return outcome

        importer = self._make_importer(kind, feishu_client)
        cases = importer.list_cases(config)
        outcome = ImportOutcome(
            import_request_id=config.get("import_request_id", ""),
            source_hash=source_hash,
            status="completed",
        )
        run_ids: list[str] = []
        for case in cases:
            case = importer.download_inputs(case, config)
            if case.errors:
                outcome.errors.extend(
                    {
                        "code": "xmax.validation",
                        "message": message,
                        "stage": "ingest",
                        "retryable": True,
                        "entity_id": case.case_number,
                    }
                    for message in case.errors
                )
                continue
            try:
                run_id = self._import_one(case, config, kind)
                run_ids.append(run_id)
                outcome.imported += 1
            except Exception as exc:
                outcome.errors.append(
                    {
                        "code": "xmax.contract_error",
                        "message": f"{case.case_number}: {exc}",
                        "stage": "ingest",
                        "retryable": False,
                        "entity_id": case.case_number,
                    }
                )

        if run_ids:
            batch_id = f"runs-{source_hash[:12]}"
            manifest = {
                "manifest_version": "1.0",
                "batch_id": batch_id,
                "entity_type": "run_batch",
                "item_entity_type": "generation_run",
                "item_ids": sorted(run_ids),
                "content_hash": content_hash({"run_ids": sorted(run_ids)}),
                "producer_stage_run_id": f"ingest-{source_hash[:8]}",
                "created_at": utc_now(),
                "metadata": {
                    "origin": _origin_for(kind),
                    "import_request_id": config.get("import_request_id"),
                    "write_remote": False,
                },
            }
            self._repository.save_batch_manifest(manifest)
            for run_id in run_ids:
                self._repository.set_run_batch_id(run_id, batch_id)
            outcome.run_batch_id = batch_id
            outcome.status = "partial" if outcome.errors else "completed"

        self._repository.save_result_import(
            config.get("import_request_id", ""),
            source_hash,
            {
                "imported": outcome.imported,
                "errors": outcome.errors,
                "run_batch_id": outcome.run_batch_id,
                "status": outcome.status,
            },
        )
        return outcome

    # ------------------------------------------------------------------
    def _make_importer(self, kind: str, feishu_client: Any) -> Any:
        if kind == "feishu_case_data":
            if feishu_client is None:
                raise MissingInputError(
                    "feishu_case_data import requires a read-only feishu client",
                    entity_id="feishu",
                    suggested_command="xmax-test ingest results --config config/existing-results.json",
                )
            return FeishuCaseImporter(feishu_client, self._download_dir)
        if kind == "local_directory":
            return LocalResultsImporter(self._download_dir)
        if kind == "stage_manifest":
            return ManifestResultsImporter(self._download_dir)
        raise ContractError(f"unknown import source kind: {kind}")

    def _import_one(self, case: Any, config: dict[str, Any], kind: str) -> str:
        result_download = case.result_download
        if not result_download:
            raise ContractError("result video missing after download")
        feed_download = case.feed_download
        prompt_download = case.prompt_attachment_download

        result_asset = self._register(result_download, "result_video")
        feed_asset = self._register(feed_download, "feed_video") if feed_download else None
        prompt_asset = (
            self._register(prompt_download, self._prompt_kind(prompt_download))
            if prompt_download
            else None
        )
        mode = self._resolve_mode(case, config)
        recipe = self._resolve_recipe(case, config)
        edited_video_asset_id, expected_audio_source_asset_id = self._resolve_edited_audio(
            recipe, case, feed_asset, prompt_asset
        )

        case_id = f"case_{content_hash({'case_number': case.case_number, 'model': case.model_version})[:16]}"
        case_record = {
            "case_id": case_id,
            "case_number": case.case_number,
            "feed_number": _number(case.case_number, "feed"),
            "prompt_number": _number(case.case_number, "prompt"),
            "feed_asset_id": feed_asset["asset_id"] if feed_asset else "",
            "prompt_asset_ids": [prompt_asset["asset_id"]] if prompt_asset else [],
            "prompt_text": case.prompt_text or "",
            "generation_mode": mode,
            "repeat_index": 1,
            "model_id": case.model_version,
            "operation_recipe_id": recipe["recipe_id"],
            "operation_recipe_version": recipe.get("version", ""),
            "edited_video_asset_id": edited_video_asset_id,
            "expected_audio_source_asset_id": expected_audio_source_asset_id,
            "api_asset_bindings": self._recipes.bindings(recipe, mode),
            "metadata": {"imported": True, "source_key": case.source_key},
        }
        self._repository.upsert_test_case(case_record, f"plan-import-{case.model_version}")

        run_id = f"run-{content_hash({'case': case_id, 'source': case.source_key})[:16]}"
        run = {
            "run_id": run_id,
            "run_batch_id": "pending",
            "case_id": case_id,
            "case_number": case.case_number,
            "status": config.get("imported_run_status", "completed"),
            "model_id": case.model_version,
            "mode": mode,
            "origin": _origin_for(kind),
            "provenance": {
                "source_type": case.provenance.get("source_type", kind),
                "source_locator": case.provenance.get("source_locator", ""),
                "source_record_id": case.source_record_id,
                "source_attachment_tokens": list(case.source_attachment_tokens),
                "source_hash": content_hash(case.provenance),
                "imported_at": utc_now(),
            },
            "result_asset_id": result_asset["asset_id"],
            "edited_video_asset_id": edited_video_asset_id,
            "expected_audio_source_asset_id": expected_audio_source_asset_id,
            "raw_events_uri": None,
            "metrics": {
                "imported": True,
                "latency_ms": None,
                "fps": None,
                "task_id": None,
                "unavailable_metrics": ["latency_ms", "fps", "task_id"],
            },
        }
        self._repository.create_run(run)
        return run_id

    def _register(self, download: dict[str, Any], kind: str) -> dict[str, Any]:
        from ..assets.models import DownloadResult

        path = Path(download["path"])
        result = DownloadResult(
            remote_key=path.name,
            path=path,
            sha256=download["sha256"],
            bytes=int(download.get("bytes", 0)),
            revision=download.get("revision"),
            attachment_token=download.get("attachment_token"),
            metadata={"kind": kind},
        )
        return self._registry.register_download(
            {"source_id": "existing-results", "kind": "import", "asset_kind": kind},
            result,
        )

    def _resolve_mode(self, case: Any, config: dict[str, Any]) -> str:
        order = config.get("mode_resolution", {}).get("order", ["explicit_field"])
        for step in order:
            if step == "explicit_field" and case.generation_mode:
                return case.generation_mode
            if step == "operation_recipe":
                recipe = self._resolve_recipe(case, config)
                return recipe.get("default_generation_mode")
            if step == "fixed_default":
                default = config.get("mode_resolution", {}).get("fixed_default")
                if default:
                    return default
        raise ContractError(
            f"cannot resolve generation mode for {case.case_number}",
            entity_id=case.case_number,
        )

    def _resolve_recipe(self, case: Any, config: dict[str, Any]) -> dict[str, Any]:
        recipe_id = case.operation_recipe_id or config.get("field_mapping", {}).get(
            "operation_recipe_id"
        )
        if recipe_id and isinstance(recipe_id, str) and recipe_id != "None":
            return self._recipes.recipe(recipe_id)
        if case.prompt_text:
            for recipe in self._recipes._pack.get("recipes", []):
                for play_name in recipe.get("play_names", []):
                    if play_name and play_name in case.prompt_text:
                        return recipe
        raise ContractError(
            f"cannot resolve operation recipe for {case.case_number}: "
            "provide operation_recipe_id or a play-name prompt",
            entity_id=case.case_number,
        )

    def _resolve_edited_audio(
        self,
        recipe: dict[str, Any],
        case: Any,
        feed_asset: dict[str, Any] | None,
        prompt_asset: dict[str, Any] | None,
    ) -> tuple[str, str]:
        edited_role = recipe.get("edited_video_role")
        audio_role = recipe.get("expected_audio_source_role")
        if edited_role == "feed_video" or edited_role == "realtime_input_stream":
            if feed_asset is None:
                raise ContractError(
                    f"{case.case_number}: recipe requires a feed video (edited video)"
                )
            edited = feed_asset["asset_id"]
        else:
            if prompt_asset is None:
                raise ContractError(
                    f"{case.case_number}: recipe requires a prompt video (edited video)"
                )
            edited = prompt_asset["asset_id"]
        audio = edited if audio_role == edited_role else feed_asset["asset_id"]
        return edited, audio

    @staticmethod
    def _prompt_kind(download: dict[str, Any]) -> str:
        filename = download.get("filename") or download.get("path") or ""
        suffix = Path(filename).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            return "prompt_image"
        return "prompt_video"

    @staticmethod
    def _snapshot(config: dict[str, Any], kind: str) -> dict[str, Any]:
        if kind == "feishu_case_data":
            return FeishuCaseImporter.snapshot(config)
        if kind == "local_directory":
            return LocalResultsImporter.snapshot(config)
        return ManifestResultsImporter.snapshot(config)


def _number(case_number: str, prefix: str) -> str:
    for part in case_number.split("_"):
        if part.startswith(prefix):
            return part
    return f"{prefix}000"


def _origin_for(kind: str) -> str:
    return {
        "feishu_case_data": "feishu_import",
        "local_directory": "local_import",
        "stage_manifest": "stage_manifest_import",
    }.get(kind, f"{kind}_import")
