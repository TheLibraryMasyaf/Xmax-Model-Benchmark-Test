"""Shared source adapter base and factory."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from xmax_test.assets.models import DownloadResult, RemoteAsset, SourceDescriptor
from xmax_test.errors import ConfigError


class BaseSource:
    """Common contract for every asset source adapter."""

    kind = "base"

    def __init__(self, descriptor: SourceDescriptor) -> None:
        self.descriptor = descriptor

    @property
    def source_id(self) -> str:
        return self.descriptor.source_id

    @property
    def asset_kind(self) -> str:
        return self.descriptor.asset_kind

    def list_assets(self, query: dict[str, Any] | None = None) -> list[RemoteAsset]:
        raise NotImplementedError

    def download(self, remote: RemoteAsset, destination: Path) -> DownloadResult:
        raise NotImplementedError


SourceFactory = Callable[[SourceDescriptor], BaseSource]


def build_source(
    item: dict[str, Any],
    *,
    client_factory: Callable[[str], Any] | None = None,
    probe: Any = None,
) -> BaseSource:
    """Create a source adapter from one asset-sources.json entry."""

    descriptor = SourceDescriptor.from_config(item)
    kind = descriptor.kind
    if kind == "local_directory":
        from .local import LocalDirectorySource

        return LocalDirectorySource(descriptor)
    if kind == "http_manifest":
        from .http import HttpManifestSource

        return HttpManifestSource(descriptor)
    if kind in {"feishu_sheet", "feishu_bitable", "feishu_wiki"}:
        if client_factory is None:
            raise ConfigError(f"source {descriptor.source_id} kind {kind} requires a feishu client")
        client = client_factory(kind)
        if kind == "feishu_sheet":
            from .feishu_sheet import FeishuSheetSource

            return FeishuSheetSource(descriptor, client)
        if kind == "feishu_bitable":
            from .feishu_bitable import FeishuBitableSource

            return FeishuBitableSource(descriptor, client)
        from .feishu_wiki import FeishuWikiSource

        return FeishuWikiSource(descriptor, client)
    raise ConfigError(f"unknown asset source kind: {kind}")
