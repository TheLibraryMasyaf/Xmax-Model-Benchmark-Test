"""HTTP(S) manifest source.

Reads a JSON manifest of ``{url, filename, revision}`` entries and downloads
each file over HTTP.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from xmax_test.errors import ExternalServiceError
from xmax_test.hashing import file_sha256
from xmax_test.assets.models import DownloadResult, RemoteAsset
from .base import BaseSource


class HttpManifestSource(BaseSource):
    kind = "http_manifest"

    def list_assets(self, query: dict[str, Any] | None = None) -> list[RemoteAsset]:
        manifest_path = Path(self.descriptor.config.get("manifest_path", ""))
        if not manifest_path.is_file():
            return []
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else data.get("entries", [])
        remotes: list[RemoteAsset] = []
        for entry in entries:
            remotes.append(
                RemoteAsset(
                    source_id=self.source_id,
                    remote_key=entry.get("filename", entry["url"].rsplit("/", 1)[-1]),
                    kind="http",
                    asset_kind=self.asset_kind,
                    revision=entry.get("revision"),
                    url=entry["url"],
                    filename=entry.get("filename"),
                    metadata={"kind": self.asset_kind},
                )
            )
        return remotes

    def download(self, remote: RemoteAsset, destination: Path) -> DownloadResult:
        url = remote.url
        if not url:
            raise ExternalServiceError(f"{self.source_id}: remote has no url")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                destination.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExternalServiceError(f"HTTP download failed for {url}: {exc}") from exc
        return DownloadResult(
            remote_key=remote.remote_key,
            path=destination,
            sha256=file_sha256(destination),
            bytes=destination.stat().st_size,
            revision=remote.revision,
            metadata={"kind": self.asset_kind},
        )
