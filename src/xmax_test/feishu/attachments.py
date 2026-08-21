"""Attachment uploader.

Artifacts intentionally use opaque internal paths such as ``source.bin``.
Those names must never leak into the Feishu business projection, so uploads
are staged under a deterministic, media-aware filename first.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..errors import ExternalServiceError
from ..hashing import file_sha256


class AttachmentUploader:
    def __init__(self, client: Any) -> None:
        self._client = client
        # A token only proves that one concrete destination cell received the
        # file. Reusing it for a different Case row would skip the actual cell
        # attachment operation, so the destination is part of the cache key.
        self._token_cache: dict[tuple[str, str, str, str, str, str], str] = {}

    def upload(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        field: str,
        artifact_uri: str,
        artifacts: Any,
        *,
        filename: str,
    ) -> dict[str, Any]:
        source = artifacts.resolve(artifact_uri)
        sha = file_sha256(source)
        safe_name = _safe_filename(filename)
        path = _stage_upload(source, artifacts, sha, safe_name)
        cache_key = (app_token, table_id, record_id, field, sha, safe_name)
        cached = self._token_cache.get(cache_key)
        if cached is not None:
            return {
                "token": cached,
                "reused": True,
                "field": field,
                "sha256": sha,
                "filename": safe_name,
            }
        try:
            result = self._client.upload_attachment(
                app_token, table_id, record_id, field, str(path)
            )
        except ExternalServiceError:
            raise
        token = result.get("token") or result.get("file_token")
        if token:
            self._token_cache[cache_key] = token
        return {
            "token": token,
            "reused": False,
            "field": field,
            "sha256": sha,
            "filename": safe_name,
        }


def media_extension(asset: dict[str, Any], path: Path) -> str:
    """Return an extension from MIME/kind and, when necessary, file magic."""

    mime = str(asset.get("mime_type") or "").lower()
    mime_extensions = {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if mime in mime_extensions:
        return mime_extensions[mime]

    header = path.read_bytes()[:32]
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return ".mp4"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"

    kind = str(asset.get("kind") or "")
    if kind.endswith("_video"):
        return ".mp4"
    if kind.endswith("_image"):
        return ".jpg"
    suffix = path.suffix.lower()
    return suffix if suffix and suffix != ".bin" else ".bin"


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    if not name or name in {".", ".."}:
        raise ExternalServiceError(f"invalid Feishu attachment filename: {filename!r}")
    return name


def _stage_upload(source: Path, artifacts: Any, sha: str, filename: str) -> Path:
    """Create a hash-verified upload alias inside the project artifact root."""

    target = artifacts.resolve(f"artifact://upload-staging/{sha[:16]}/{filename}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and file_sha256(target) == sha:
        return target
    temp = target.parent / f".xmax-upload-{uuid.uuid4().hex}"
    try:
        shutil.copyfile(source, temp)
        if file_sha256(temp) != sha:
            raise ExternalServiceError(f"staged upload hash mismatch: {source}")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target
