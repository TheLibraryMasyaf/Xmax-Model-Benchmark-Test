"""Local directory source."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xmax_test.hashing import file_sha256
from xmax_test.assets.models import DownloadResult, RemoteAsset
from .base import BaseSource


class LocalDirectorySource(BaseSource):
    kind = "local_directory"

    def list_assets(self, query: dict[str, Any] | None = None) -> list[RemoteAsset]:
        directory = Path(self.descriptor.config.get("path", ""))
        include = self.descriptor.config.get("include", ["*"])
        if not directory.is_dir():
            return []
        remotes: list[RemoteAsset] = []
        for pattern in include:
            for path in sorted(directory.glob(pattern)):
                if not path.is_file():
                    continue
                remotes.append(
                    RemoteAsset(
                        source_id=self.source_id,
                        remote_key=path.name,
                        kind="file",
                        asset_kind=self.asset_kind,
                        revision=str(path.stat().st_mtime_ns),
                        filename=path.name,
                        bytes=path.stat().st_size,
                        metadata={"path": str(path)},
                    )
                )
        return remotes

    def download(self, remote: RemoteAsset, destination: Path) -> DownloadResult:
        source_path = Path(remote.metadata.get("path", remote.remote_key))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source_path.read_bytes())
        return DownloadResult(
            remote_key=remote.remote_key,
            path=destination,
            sha256=file_sha256(destination),
            bytes=destination.stat().st_size,
            revision=remote.revision,
            metadata={"kind": self.asset_kind},
        )
