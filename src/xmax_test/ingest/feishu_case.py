"""Feishu Case-data importer (read-only).

Fully pages the Case table, downloads and validates result/Feed/Prompt inputs,
then hands normalized records to the ImportService. Importing never writes
remote.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import ContractError, ExternalServiceError
from .models import ImportedCase


class FeishuCaseImporter:
    def __init__(self, client: Any, download_dir: Path) -> None:
        self._client = client
        self._download_dir = Path(download_dir)
        self._download_dir.mkdir(parents=True, exist_ok=True)

    def list_cases(self, config: dict[str, Any]) -> list[ImportedCase]:
        source = config.get("source", {})
        app_token = source.get("base_token", "")
        table_id = source.get("table_id", "")
        mapping = config.get("field_mapping", {})
        selector_match = config.get("selector", {}).get("match", {})
        filters = selector_match.get("filters", {})
        model_versions = filters.get("model_versions") or []
        case_numbers = filters.get("case_numbers") or []

        records = self._page_all(app_token, table_id)
        cases: list[ImportedCase] = []
        for record in records:
            fields = record.get("fields", {})
            case_number = _field(fields, mapping.get("case_number"))
            model_version = _field(fields, mapping.get("model_version"))
            if case_numbers and case_number not in case_numbers:
                continue
            if model_versions and model_version not in model_versions:
                continue
            cases.append(
                ImportedCase(
                    source_key=str(record.get("record_id", case_number)),
                    case_number=str(case_number),
                    model_version=str(model_version),
                    prompt_text=_field(fields, mapping.get("prompt_text")),
                    generation_mode=_field(fields, mapping.get("generation_mode")),
                    operation_recipe_id=_field(fields, mapping.get("operation_recipe_id")),
                    source_record_id=record.get("record_id"),
                    source_attachment_tokens=tuple(
                        _attachment_tokens(fields, mapping.get("result_attachment"))
                    ),
                    provenance={
                        "source_type": "feishu_case_data",
                        "source_locator": f"{app_token}/{table_id}",
                        "source_record_id": record.get("record_id"),
                        "case_number": case_number,
                        "record_fields_snapshot": _snapshot_fields(fields, mapping),
                    },
                )
            )
        return cases

    def download_inputs(self, case: ImportedCase, config: dict[str, Any]) -> ImportedCase:
        """Download result/feed/prompt attachments for one case."""

        mapping = config.get("field_mapping", {})
        downloads = config.get("download", {})
        fields = case.provenance.get("record_fields_snapshot", {})
        if downloads.get("result_video"):
            case = self._download_field(
                case,
                fields,
                mapping.get("result_attachment"),
                "result_video",
                "result",
                config,
            )
        if downloads.get("feed_and_prompt_inputs"):
            case = self._download_field(
                case,
                fields,
                mapping.get("feed_attachment"),
                "feed_video",
                "feed",
                config,
            )
            case = self._download_field(
                case,
                fields,
                mapping.get("prompt_attachment"),
                "prompt_video",
                "prompt",
                config,
            )
        return case

    def _download_field(
        self,
        case: ImportedCase,
        fields: dict[str, Any],
        source_field: str | None,
        kind: str,
        role: str,
        config: dict[str, Any],
    ) -> ImportedCase:
        token = _first_token(fields, source_field)
        if not token:
            if role == "result":
                case.errors.append(f"missing result attachment for {case.case_number}")
            return case
        target = self._download_dir / f"{case.source_key}_{role}.bin"
        try:
            source = config.get("source", {})
            raw = self._client.download_attachment(
                token,
                target,
                app_token=source.get("base_token"),
                table_id=source.get("table_id"),
                record_id=case.source_record_id,
            )
        except ExternalServiceError as exc:
            case.errors.append(f"{role} download failed: {exc}")
            return case
        download = {
            "path": raw["path"],
            "sha256": raw["sha256"],
            "bytes": raw["bytes"],
            "kind": kind,
            "attachment_token": token,
            "filename": _first_item_name(fields, source_field),
        }
        data = case.__dict__.copy()
        if role == "result":
            data["result_download"] = download
        elif role == "feed":
            data["feed_download"] = download
        else:
            data["prompt_attachment_download"] = download
        return ImportedCase(**data)

    def _page_all(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            page = self._client.get_bitable_records(app_token, table_id, page_token=page_token)
            records.extend(page.get("records", []))
            if not page.get("has_more"):
                break
            page_token = page.get("page_token")
            if not page_token:
                raise ContractError(f"feishu case table {table_id} has_more but no page_token")
        return records

    @staticmethod
    def snapshot(config: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": config.get("source", {}).get("kind"),
            "base_token": config.get("source", {}).get("base_token"),
            "table_id": config.get("source", {}).get("table_id"),
            "field_mapping": config.get("field_mapping", {}),
            "selector": config.get("selector", {}),
        }


def _field(fields: dict[str, Any], name: Any) -> str | None:
    if not name:
        return None
    value = fields.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return str(value[0].get("text", ""))
    if value is None:
        return None
    return str(value)


def _attachment_tokens(fields: dict[str, Any], name: Any) -> list[str]:
    if not name:
        return []
    value = fields.get(name)
    if isinstance(value, list):
        tokens = []
        for item in value:
            if isinstance(item, dict) and item.get("file_token"):
                tokens.append(item["file_token"])
        return tokens
    if isinstance(value, dict) and value.get("file_token"):
        return [value["file_token"]]
    return []


def _first_token(fields: dict[str, Any], name: Any) -> str | None:
    tokens = _attachment_tokens(fields, name)
    return tokens[0] if tokens else None


def _first_item_name(fields: dict[str, Any], name: Any) -> str | None:
    if not name:
        return None
    value = fields.get(name)
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("name"):
                return item["name"]
    if isinstance(value, dict) and value.get("name"):
        return value["name"]
    return None


def _snapshot_fields(fields: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    """Keep only the mapped fields plus attachment tokens for provenance."""

    snapshot: dict[str, Any] = {}
    for entity_field, source_field in mapping.items():
        if not source_field:
            continue
        value = fields.get(source_field)
        if isinstance(value, list):
            snapshot[source_field] = [
                {
                    "text": item.get("text"),
                    "file_token": item.get("file_token"),
                    "name": item.get("name"),
                }
                for item in value
                if isinstance(item, dict)
            ]
        else:
            snapshot[source_field] = value
    return snapshot
