"""Feishu Bitable (multi-dimensional table) source.

Paginates records until ``has_more=false``. Text fields and attachment fields
are extracted through the source's field mapping.
"""

from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from xmax_test.errors import ContractError
from xmax_test.hashing import file_sha256
from xmax_test.assets.models import DownloadResult, RemoteAsset
from .base import BaseSource


class FeishuBitableSource(BaseSource):
    kind = "feishu_bitable"

    def __init__(self, descriptor: Any, client: Any) -> None:
        super().__init__(descriptor)
        self._client = client
        self._app_token = descriptor.config.get("app_token", "")
        self._table_id = descriptor.config.get("table_id", "")
        self._mapping = descriptor.config.get("field_mapping", {})
        self._selection_field = descriptor.config.get("selection_field")
        self._selection_value = descriptor.config.get("selection_value")

    def list_assets(self, query: dict[str, Any] | None = None) -> list[RemoteAsset]:
        remotes: list[RemoteAsset] = []
        page_token: str | None = None
        pages = 0
        while True:
            page = self._client.get_bitable_records(
                self._app_token, self._table_id, page_token=page_token
            )
            pages += 1
            for record in page.get("records", []):
                if not self._selected(record.get("fields", {})):
                    continue
                remotes.extend(self._record_assets(record))
            if not page.get("has_more"):
                break
            page_token = page.get("page_token")
            if not page_token:
                raise ContractError(
                    f"bitable {self._table_id} has_more=true but no next page_token"
                )
        return remotes

    def _selected(self, fields: dict[str, Any]) -> bool:
        if not self._selection_field:
            return True
        value = fields.get(self._selection_field)
        if isinstance(value, list):
            return self._selection_value in value
        return value == self._selection_value

    def _record_assets(self, record: dict[str, Any]) -> list[RemoteAsset]:
        record_id = record.get("record_id")
        fields = record.get("fields", {})
        remotes: list[RemoteAsset] = []
        shared_metadata = {
            "record_id": record_id,
            "record_number": _scalar(fields.get(self._mapping.get("record_number", ""))),
            "play_name": _scalar(fields.get(self._mapping.get("play_name", ""))),
            "scenario_id": _scalar(fields.get(self._mapping.get("scenario_id", ""))),
            "test_tags": _scalar(fields.get(self._mapping.get("test_tags", ""))),
            "content_tags": _scalar(fields.get(self._mapping.get("content_tags", ""))),
        }
        metadata_keys = {"record_number", "play_name", "scenario_id", "test_tags", "content_tags"}
        for entity_field, source_field in self._mapping.items():
            if entity_field in metadata_keys:
                continue
            value = fields.get(source_field)
            if value is None:
                continue
            if isinstance(value, list):
                for index, item in enumerate(value):
                    asset = self._attachment_asset(
                        record_id, entity_field, index, item, shared_metadata
                    )
                    if asset is not None:
                        remotes.append(asset)
            elif isinstance(value, str):
                if value.strip():
                    remotes.append(
                        RemoteAsset(
                            source_id=self.source_id,
                            remote_key=f"{record_id}_{entity_field}",
                            kind="bitable_text",
                            asset_kind="prompt_text",
                            record_id=record_id,
                            metadata={
                                "kind": "prompt_text",
                                "record_id": record_id,
                                "group_id": record_id,
                                "field": source_field,
                                "text": value,
                                **shared_metadata,
                            },
                        )
                    )
        return remotes

    def _attachment_asset(
        self, record_id: str, entity_field: str, index: int, item: Any,
        shared_metadata: dict[str, Any],
    ) -> RemoteAsset | None:
        if not isinstance(item, dict):
            return None
        token = item.get("file_token") or item.get("token")
        if not token:
            return None
        asset_kind = entity_field
        if entity_field in {"prompt_reference", "feed_reference"}:
            name = str(item.get("name") or item.get("file_name") or "").lower()
            prefix = "prompt" if entity_field == "prompt_reference" else "feed"
            asset_kind = (
                f"{prefix}_video"
                if name.endswith((".mp4", ".mov", ".webm"))
                else f"{prefix}_image"
            )
        return RemoteAsset(
            source_id=self.source_id,
            remote_key=f"{record_id}_{entity_field}_{index}",
            kind="bitable_attachment",
            asset_kind=asset_kind,
            attachment_token=token,
            record_id=record_id,
            filename=item.get("name") or item.get("file_name"),
            metadata={
                "kind": asset_kind,
                "record_id": record_id,
                "group_id": record_id,
                **shared_metadata,
            },
        )

    def download(self, remote: RemoteAsset, destination: Path) -> DownloadResult:
        token = remote.attachment_token
        if remote.kind == "bitable_text":
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "record_id": remote.record_id,
                "text": remote.metadata.get("text", ""),
            }
            destination.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            return DownloadResult(
                remote_key=remote.remote_key,
                path=destination,
                sha256=file_sha256(destination),
                bytes=destination.stat().st_size,
                metadata=remote.metadata,
            )
        if not token:
            raise ContractError(
                f"bitable remote {remote.remote_key} has no attachment token"
            )
        if destination.is_file() and destination.stat().st_size > 0:
            return DownloadResult(
                remote_key=remote.remote_key,
                path=destination,
                sha256=file_sha256(destination),
                bytes=destination.stat().st_size,
                attachment_token=token,
                metadata=remote.metadata,
            )
        raw = self._client.download_attachment(
            token, destination, app_token=self._app_token,
            table_id=self._table_id, record_id=remote.record_id,
        )
        return DownloadResult(
            remote_key=remote.remote_key,
            path=Path(raw["path"]),
            sha256=raw["sha256"],
            bytes=int(raw["bytes"]),
            attachment_token=token,
            metadata=remote.metadata,
        )


def _scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return "、".join(str(item) for item in value if item is not None)
    return str(value)
