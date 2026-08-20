"""Feishu spreadsheet (Sheet) source.

Sheet reads must check ``has_more``, ``truncated``, ``actual_range``,
``row_indices`` and ``col_indices``. Real row numbers come from
``row_indices``; ``record_id``/attachment tokens are carried through so later
sync can locate the exact cell.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xmax_test.errors import ContractError
from xmax_test.hashing import file_sha256
from xmax_test.assets.models import DownloadResult, RemoteAsset
from .base import BaseSource


class FeishuSheetSource(BaseSource):
    kind = "feishu_sheet"

    def __init__(self, descriptor: Any, client: Any) -> None:
        super().__init__(descriptor)
        self._client = client
        self._spreadsheet_token = descriptor.config.get("spreadsheet_token", "")
        self._sheet_id = descriptor.config.get("sheet_id", "")
        self._range = descriptor.config.get("range", "A1:ZZ100000")
        self._mapping = descriptor.config.get("field_mapping", {})

    def list_assets(self, query: dict[str, Any] | None = None) -> list[RemoteAsset]:
        meta = self._client.get_sheet_meta(
            self._spreadsheet_token, self._sheet_id, self._range
        )
        if meta.get("truncated"):
            raise ContractError(
                f"sheet {self._sheet_id} response truncated; cannot report a "
                "complete source snapshot"
            )
        if meta.get("has_more"):
            raise ContractError(
                f"sheet {self._sheet_id} has more rows; range must cover the "
                "full effective area"
            )
        rows = meta.get("rows", [])
        row_indices = meta.get("row_indices", [])
        col_indices = meta.get("col_indices", [])
        if len(rows) != len(row_indices):
            raise ContractError(
                f"sheet {self._sheet_id} row count {len(rows)} does not match "
                f"row_indices {len(row_indices)}"
            )
        remotes: list[RemoteAsset] = []
        for index, (row, row_index) in enumerate(zip(rows, row_indices)):
            for col_offset, column in enumerate(row or []):
                value = _value(column)
                if value is None:
                    continue
                asset_kind, token = self._classify(column)
                remotes.append(
                    RemoteAsset(
                        source_id=self.source_id,
                        remote_key=f"row{row_index}_col{col_indices[col_offset] if col_offset < len(col_indices) else col_offset}",
                        kind="sheet_cell",
                        asset_kind=asset_kind or self.asset_kind,
                        revision=str(row_index),
                        attachment_token=token,
                        row_index=int(row_index),
                        metadata={
                            "kind": asset_kind or self.asset_kind,
                            "row_index": int(row_index),
                            "col_index": col_indices[col_offset] if col_offset < len(col_indices) else col_offset,
                            "value": value if isinstance(value, str) else "",
                        },
                    )
                )
        return remotes

    def download(self, remote: RemoteAsset, destination: Path) -> DownloadResult:
        token = remote.attachment_token
        if token:
            raw = self._client.download_attachment(token, destination)
            return DownloadResult(
                remote_key=remote.remote_key,
                path=Path(raw["path"]),
                sha256=raw["sha256"],
                bytes=int(raw["bytes"]),
                revision=remote.revision,
                attachment_token=token,
                metadata={"kind": remote.asset_kind},
            )
        # Plain text cells have nothing to download; caller decides to skip.
        raise ContractError(
            f"sheet cell {remote.remote_key} has no attachment token",
            entity_id=remote.remote_key,
        )

    def _classify(self, column: Any) -> tuple[str | None, str | None]:
        if not isinstance(column, dict):
            return None, None
        token = column.get("file_token") or column.get("token")
        attachment = column.get("attachment") or column.get("file")
        if isinstance(attachment, dict):
            token = token or attachment.get("file_token")
        if token:
            return self.asset_kind, token
        return None, None


def _value(column: Any) -> Any:
    if isinstance(column, dict):
        return column.get("text") or column.get("value")
    return column
