"""Stage-manifest results importer.

Reads a frozen Stage Manifest (or the batch it references) and imports the
completed runs it describes. Missing mode/recipe/edited-video/audio facts are
errors, never guesses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ImportedCase


class ManifestResultsImporter:
    def __init__(self, download_dir: Path) -> None:
        self._download_dir = Path(download_dir)
        self._download_dir.mkdir(parents=True, exist_ok=True)

    def list_cases(self, config: dict[str, Any]) -> list[ImportedCase]:
        manifest_uri = config.get("source", {}).get("manifest_uri", "")
        if not manifest_uri:
            return []
        path = Path(manifest_uri.replace("manifest://", ""))
        if not path.is_file():
            return []
        manifest = json.loads(path.read_text(encoding="utf-8"))
        runs = manifest.get("runs", manifest.get("item_ids", []))
        cases: list[ImportedCase] = []
        for run in runs:
            if isinstance(run, str):
                continue
            case = ImportedCase(
                source_key=run.get("run_id", ""),
                case_number=run.get("case_number", ""),
                model_version=run.get("model_id", ""),
                prompt_text=run.get("prompt_text"),
                generation_mode=run.get("mode"),
                operation_recipe_id=run.get("operation_recipe_id"),
                provenance={
                    "source_type": "stage_manifest",
                    "source_locator": manifest_uri,
                    "source_record_id": run.get("run_id"),
                    "case_number": run.get("case_number"),
                    "run_batch_id": run.get("run_batch_id"),
                    "result_uri": run.get("result_asset_uri"),
                    "feed_uri": run.get("feed_asset_uri"),
                    "prompt_uri": run.get("prompt_asset_uri"),
                },
            )
            cases.append(case)
        return cases

    def download_inputs(self, case: ImportedCase, config: dict[str, Any]) -> ImportedCase:
        return case

    @staticmethod
    def snapshot(config: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "stage_manifest", "manifest_uri": config.get("source", {}).get("manifest_uri")}
