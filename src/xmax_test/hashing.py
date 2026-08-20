"""Canonical JSON and SHA-256 hashing.

JSON hashing is fixed to UTF-8, sorted keys and compact separators. Reproducible
business keys never include local absolute paths, secrets or creation time.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize to a canonical JSON string with sorted keys."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(value: Any) -> str:
    """Stable content hash for any JSON-serializable value."""

    return sha256_text(canonical_json(value))


def file_sha256(path: str | object) -> str:
    """Hash a file incrementally without loading it fully into memory."""

    from pathlib import Path

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_path(path: str | object) -> str:
    """Convenience alias mirroring the Asset manifest's sha256 field."""

    return file_sha256(path)
