"""Local directory results importer.

Scans a directory for result videos plus feed/prompt inputs. File naming is
controlled: ``<case_number>.mp4`` (result), ``<case_number>.feed.<ext>`` and
``<case_number>.prompt.<ext>``; a ``manifest.json`` in the directory may carry
explicit mapping metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ImportedCase

RESULT_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")


class LocalResultsImporter:
    def __init__(self, download_dir: Path) -> None:
        self._download_dir = Path(download_dir)
        self._download_dir.mkdir(parents=True, exist_ok=True)

    def list_cases(self, config: dict[str, Any]) -> list[ImportedCase]:
        directory = Path(config.get("source", {}).get("directory", ""))
        if not directory.is_dir():
            return []
        manifest_path = directory / "manifest.json"
        metadata: dict[str, Any] = {}
        if manifest_path.is_file():
            metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
        items = metadata.get("cases", [])
        cases: list[ImportedCase] = []
        if items:
            for item in items:
                cases.append(self._case_from_metadata(item, directory))
        else:
            for result in sorted(directory.glob("*.mp4")) + sorted(directory.glob("*.mov")):
                cases.append(self._case_from_file(result, directory))
        return cases

    def download_inputs(self, case: ImportedCase, config: dict[str, Any]) -> ImportedCase:
        downloads = config.get("download", {})
        if downloads.get("result_video") and case.result_download is None and case.provenance.get("result_path"):
            source = Path(case.provenance["result_path"])
            case = self._copy_into(case, source, "result")
        if downloads.get("feed_and_prompt_inputs"):
            if case.feed_download is None and case.provenance.get("feed_path"):
                case = self._copy_into(case, Path(case.provenance["feed_path"]), "feed")
            if case.prompt_attachment_download is None and case.provenance.get("prompt_path"):
                case = self._copy_into(case, Path(case.provenance["prompt_path"]), "prompt")
        return case

    def _copy_into(self, case: ImportedCase, source: Path, role: str) -> ImportedCase:
        from ..hashing import file_sha256

        if not source.is_file():
            case.errors.append(f"{role} input missing: {source}")
            return case
        target = self._download_dir / f"{case.source_key}_{role}{source.suffix}"
        target.write_bytes(source.read_bytes())
        download = {
            "path": str(target),
            "sha256": file_sha256(target),
            "bytes": target.stat().st_size,
            "kind": {"result": "result_video", "feed": "feed_video", "prompt": "prompt_video"}[role],
        }
        data = case.__dict__.copy()
        if role == "result":
            data["result_download"] = download
        elif role == "feed":
            data["feed_download"] = download
        else:
            data["prompt_attachment_download"] = download
        return ImportedCase(**data)

    @staticmethod
    def _case_from_metadata(item: dict[str, Any], directory: Path) -> ImportedCase:
        return ImportedCase(
            source_key=item.get("source_key", item.get("case_number", "")),
            case_number=item.get("case_number", ""),
            model_version=item.get("model_version", ""),
            prompt_text=item.get("prompt_text"),
            generation_mode=item.get("generation_mode"),
            operation_recipe_id=item.get("operation_recipe_id"),
            provenance={
                "source_type": "local_directory",
                "source_locator": str(directory),
                "result_path": str(directory / item["result_file"]) if item.get("result_file") else None,
                "feed_path": str(directory / item["feed_file"]) if item.get("feed_file") else None,
                "prompt_path": str(directory / item["prompt_file"]) if item.get("prompt_file") else None,
                "case_number": item.get("case_number"),
            },
        )

    @staticmethod
    def _case_from_file(result: Path, directory: Path) -> ImportedCase:
        case_number = result.stem
        feed_candidates = [
            directory / f"{case_number}.feed{result.suffix}",
            directory / f"{case_number}.feed.mp4",
        ]
        feed_path = next((p for p in feed_candidates if p.is_file()), None)
        prompt_candidates = [
            directory / f"{case_number}.prompt{result.suffix}",
            directory / f"{case_number}.prompt.mp4",
            directory / f"{case_number}.prompt.jpg",
            directory / f"{case_number}.prompt.png",
        ]
        prompt_path = next((p for p in prompt_candidates if p.is_file()), None)
        return ImportedCase(
            source_key=case_number,
            case_number=case_number,
            model_version="",
            prompt_text=None,
            provenance={
                "source_type": "local_directory",
                "source_locator": str(directory),
                "result_path": str(result),
                "feed_path": str(feed_path) if feed_path else None,
                "prompt_path": str(prompt_path) if prompt_path else None,
                "case_number": case_number,
            },
        )

    @staticmethod
    def snapshot(config: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "local_directory", "directory": config.get("source", {}).get("directory")}
