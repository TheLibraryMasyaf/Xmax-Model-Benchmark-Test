"""Feishu Wiki source.

Resolves a wiki node to its real object (Bitable / Sheet / Docx / File) and
delegates to the matching adapter. The remote object type is never guessed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xmax_test.assets.models import DownloadResult, RemoteAsset
from xmax_test.errors import ContractError, ExternalServiceError

from .base import BaseSource


class FeishuWikiSource(BaseSource):
    kind = "feishu_wiki"

    def __init__(self, descriptor: Any, client: Any) -> None:
        super().__init__(descriptor)
        self._client = client
        self._wiki_token = descriptor.config.get("wiki_token", "")
        self._target = self._resolve_target()

    def _resolve_target(self) -> dict[str, Any]:
        node = self._client.get_wiki_node(self._wiki_token)
        obj_type = node.get("obj_type")
        obj_token = node.get("obj_token")
        if not obj_type or not obj_token:
            raise ContractError(f"wiki node {self._wiki_token} returned no obj_type/obj_token")
        return {"obj_type": obj_type, "obj_token": obj_token}

    def list_assets(self, query: dict[str, Any] | None = None) -> list[RemoteAsset]:
        target = self._target
        if target["obj_type"] == "bitable":
            return self._bitable().list_assets(query)
        if target["obj_type"] == "sheet":
            return self._sheet().list_assets(query)
        if target["obj_type"] in {"docx", "file"}:
            # Document/file wiki nodes expose attachments through a token;
            # without an attachment mapping there is nothing to list.
            return []
        raise ExternalServiceError(
            f"wiki node {self._wiki_token} has unsupported obj_type {target['obj_type']!r}"
        )

    def download(self, remote: RemoteAsset, destination: Path) -> DownloadResult:
        if remote.kind == "bitable_attachment":
            return self._bitable().download(remote, destination)
        if remote.kind == "sheet_cell":
            return self._sheet().download(remote, destination)
        raise ContractError(f"wiki download unsupported for kind {remote.kind!r}")

    def _bitable(self) -> Any:
        from .feishu_bitable import FeishuBitableSource

        descriptor = _clone_descriptor(self.descriptor, self._target["obj_token"])
        return FeishuBitableSource(descriptor, self._client)

    def _sheet(self) -> Any:
        from .feishu_sheet import FeishuSheetSource

        descriptor = _clone_descriptor(self.descriptor, self._target["obj_token"])
        return FeishuSheetSource(descriptor, self._client)


def _clone_descriptor(descriptor: Any, token: str) -> Any:
    config = dict(descriptor.config)
    config["app_token"] = token
    config["spreadsheet_token"] = token
    return type(descriptor)(
        descriptor.source_id,
        descriptor.kind,
        descriptor.enabled,
        descriptor.asset_kind,
        config,
    )
