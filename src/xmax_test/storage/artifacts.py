"""Artifact store for large files.

Local URIs are ``artifact://<namespace>/<relative-path>``. Absolute host paths
never appear in cross-machine contracts; the CLI may additionally print the
resolved local path. Names and directories use stable IDs and controlled
suffixes only.

Writes are atomic: data goes to a same-directory temp file, is hash-verified,
then renamed into place. Failed temp files remain on a cleanable list.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..errors import ContractError, ValidationError
from ..hashing import file_sha256, sha256_bytes
from ..time import utc_now

URI_PATTERN = re.compile(r"^artifact://([^/]+)/(.+)$")
TEMP_PREFIX = ".xmax-tmp-"


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def uri(self, namespace: str, relative_path: str) -> str:
        self._validate_namespace(namespace)
        self._validate_relative(relative_path)
        return f"artifact://{namespace}/{relative_path}"

    def resolve(self, uri: str) -> Path:
        """Resolve an artifact URI to a local path, rejecting traversal."""

        namespace, relative = self._parse_uri(uri)
        target = (self._root / namespace / relative).resolve()
        if not str(target).startswith(str(self._root) + os.sep) and target != self._root:
            raise ContractError(f"artifact URI escapes artifact root: {uri}")
        return target

    def put_file(
        self,
        namespace: str,
        source: Path,
        relative_path: str,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Store a file atomically and return uri/sha256/bytes."""

        self._validate_namespace(namespace)
        self._validate_relative(relative_path)
        source = Path(source)
        if not source.is_file():
            raise ValidationError(f"artifact source missing: {source}")
        actual_hash = file_sha256(source)
        if expected_sha256 and actual_hash != expected_sha256:
            raise ValidationError(
                f"source hash mismatch for {source}: expected {expected_sha256}, "
                f"got {actual_hash}"
            )
        target = self._root / namespace / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f"{TEMP_PREFIX}{uuid.uuid4().hex}"
        try:
            shutil.copyfile(source, temp)
            if file_sha256(temp) != actual_hash:
                raise ValidationError(f"artifact copy corrupt: {source}")
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
        return {
            "uri": self.uri(namespace, relative_path),
            "sha256": actual_hash,
            "bytes": target.stat().st_size,
        }

    def put_bytes(
        self,
        namespace: str,
        relative_path: str,
        data: bytes,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        self._validate_namespace(namespace)
        self._validate_relative(relative_path)
        actual_hash = sha256_bytes(data)
        if expected_sha256 and actual_hash != expected_sha256:
            raise ValidationError(
                f"payload hash mismatch: expected {expected_sha256}, got {actual_hash}"
            )
        target = self._root / namespace / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f"{TEMP_PREFIX}{uuid.uuid4().hex}"
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
        return {
            "uri": self.uri(namespace, relative_path),
            "sha256": actual_hash,
            "bytes": len(data),
        }

    def write_atomic(self, uri: str, data: bytes) -> str:
        """Overwrite an existing artifact atomically; returns the URI."""

        target = self.resolve(uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f"{TEMP_PREFIX}{uuid.uuid4().hex}"
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
        return uri

    def verify(self, uri: str, expected_sha256: str | None = None) -> dict[str, Any]:
        """Check an artifact exists and its hash matches."""

        path = self.resolve(uri)
        if not path.is_file():
            raise ValidationError(f"artifact missing: {uri}")
        actual = file_sha256(path)
        if expected_sha256 and actual != expected_sha256:
            raise ValidationError(
                f"artifact hash mismatch for {uri}: expected {expected_sha256}, "
                f"got {actual}"
            )
        return {"uri": uri, "sha256": actual, "bytes": path.stat().st_size, "ok": True}

    def read_bytes(self, uri: str) -> bytes:
        return self.resolve(uri).read_bytes()

    def list(self, namespace: str | None = None) -> list[str]:
        """Return URIs under the store, skipping temp files."""

        base = self._root / namespace if namespace else self._root
        if not base.is_dir():
            return []
        uris: list[str] = []
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.name.startswith(TEMP_PREFIX):
                relative = path.relative_to(self._root).as_posix()
                namespace_part, _, rest = relative.partition("/")
                uris.append(f"artifact://{namespace_part}/{rest}")
        return uris

    def gc(self, *, dry_run: bool = True) -> list[dict[str, Any]]:
        """Report (and optionally delete) leftover temp files.

        Physical deletion only happens with an explicit non-dry call; the
        returned manifest is the deletion record.
        """

        candidates = [
            path for path in self._root.rglob(f"{TEMP_PREFIX}*") if path.is_file()
        ]
        result: list[dict[str, Any]] = []
        for path in sorted(candidates):
            entry = {
                "path": str(path),
                "bytes": path.stat().st_size,
                "deleted": False,
                "removed_at": None,
            }
            if not dry_run:
                path.unlink(missing_ok=True)
                entry["deleted"] = True
                entry["removed_at"] = utc_now()
            result.append(entry)
        return result

    @staticmethod
    def _parse_uri(uri: str) -> tuple[str, str]:
        match = URI_PATTERN.match(uri)
        if not match:
            raise ContractError(f"invalid artifact URI: {uri}")
        return match.group(1), match.group(2)

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", namespace):
            raise ContractError(f"invalid artifact namespace: {namespace!r}")

    @staticmethod
    def _validate_relative(relative_path: str) -> None:
        if not relative_path or relative_path.startswith("/") or ".." in relative_path.split("/"):
            raise ContractError(f"invalid artifact relative path: {relative_path!r}")
