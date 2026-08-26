"""Feishu sync client boundary (read-write).

The sync client reads the real table structure before writing, upserts
records, uploads attachments and supports full read-back for reconciliation.
The fake keeps everything offline and scriptable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..errors import ExternalServiceError, MissingDependencyError
from ..hashing import file_sha256
from .record_pages import normalize_record_page


class FeishuSyncClient(Protocol):
    def get_fields(self, app_token: str, table_id: str) -> list[dict[str, Any]]: ...

    def list_records(
        self, app_token: str, table_id: str, page_token: str | None = None
    ) -> dict[str, Any]: ...

    def upsert_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str | None,
        fields: dict[str, Any],
    ) -> dict[str, Any]: ...

    def find_record(
        self,
        app_token: str,
        table_id: str,
        key_field: str,
        key_value: str,
        secondary_field: str,
        secondary_value: str,
        field_names: list[str] | None = None,
    ) -> dict[str, Any] | None: ...

    def upload_attachment(
        self, app_token: str, table_id: str, record_id: str, field: str, path: str
    ) -> dict[str, Any]: ...

    def remove_attachments(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        field: str,
        file_tokens: list[str],
    ) -> dict[str, Any]: ...

    def download_attachment(self, token: str, destination: Path) -> dict[str, Any]: ...


class LarkCliSyncClient:
    """Real sync client over lark-cli (``--as user`` by default)."""

    def __init__(
        self, binary: str = "lark-cli", identity: str = "user", timeout: int = 120
    ) -> None:
        self._binary = binary
        self._identity = identity
        self._timeout = timeout

    def _run(self, args: list[str]) -> dict[str, Any]:
        import json
        import subprocess
        from shutil import which

        if which(self._binary) is None:
            raise MissingDependencyError(f"lark-cli binary not found: {self._binary}")
        result = subprocess.run(
            [self._binary] + args + ["--as", self._identity, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if result.returncode != 0:
            raise ExternalServiceError(f"lark-cli exited {result.returncode}: {result.stderr}")
        payload = json.loads(result.stdout)
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise ExternalServiceError(f"lark-cli failure: {payload.get('error')}")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def get_fields(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        data = self._run(
            [
                "base",
                "+field-list",
                "--base-token",
                app_token,
                "--table-id",
                table_id,
                "--limit",
                "200",
            ]
        )
        if isinstance(data, list):
            return data
        return data.get("items") or data.get("fields") or []

    def list_records(
        self,
        app_token: str,
        table_id: str,
        page_token: str | None = None,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        offset = int(page_token or 0)
        args = [
                "base",
                "+record-list",
                "--base-token",
                app_token,
                "--table-id",
                table_id,
                "--offset",
                str(offset),
                "--limit",
                "200",
            ]
        if view_id:
            args.extend(["--view-id", view_id])
        data = self._run(args)
        return normalize_record_page(data, offset=offset)

    def get_bitable_records(
        self,
        app_token: str,
        table_id: str,
        page_token: str | None = None,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        """Alias used by the read-only case importer."""
        return self.list_records(
            app_token, table_id, page_token=page_token, view_id=view_id
        )

    def upsert_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str | None,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        import json

        args = [
            "base",
            "+record-upsert",
            "--base-token",
            app_token,
            "--table-id",
            table_id,
        ]
        if record_id:
            args += ["--record-id", record_id]
        args += ["--json", json.dumps(fields, ensure_ascii=False)]
        data = self._run(args)
        if isinstance(data, dict) and "record_id" not in data:
            record = data.get("record") or {}
            if isinstance(record, dict):
                data = {**data, **record}
        # lark-cli 1.0.80 does not echo the ID for an explicit update.
        if isinstance(data, dict) and record_id and not data.get("record_id"):
            data["record_id"] = record_id
        return data

    def find_record(
        self,
        app_token: str,
        table_id: str,
        key_field: str,
        key_value: str,
        secondary_field: str,
        secondary_value: str,
        field_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Find one Case row and return every field needed for idempotent sync.

        Attachment sync must see the existing attachment cells. Returning only
        the two key fields makes a resumed ``full`` sync misread an existing
        row as attachment-free and append the same files again.
        """

        selected_fields = list(
            dict.fromkeys([key_field, secondary_field, *(field_names or [])])
        )
        args = [
                "base",
                "+record-search",
                "--base-token",
                app_token,
                "--table-id",
                table_id,
                "--keyword",
                key_value,
                "--search-field",
                key_field,
                "--limit",
                "20",
            ]
        for field in selected_fields:
            args.extend(["--field-id", field])
        data = self._run(args)
        rows = data.get("data") or []
        names = data.get("fields") or []
        record_ids = data.get("record_id_list") or []
        for index, row in enumerate(rows):
            fields = {
                str(name): row[column] if column < len(row) else None
                for column, name in enumerate(names)
            }
            if (
                str(fields.get(key_field, "")) == str(key_value)
                and str(fields.get(secondary_field, "")) == str(secondary_value)
                and index < len(record_ids)
            ):
                return {"record_id": record_ids[index], "fields": fields}
        return None

    def upload_attachment(
        self, app_token: str, table_id: str, record_id: str, field: str, path: str
    ) -> dict[str, Any]:
        safe_path = self._safe_upload_path(path)
        return self._run(
            [
                "base",
                "+record-upload-attachment",
                "--base-token",
                app_token,
                "--table-id",
                table_id,
                "--record-id",
                record_id,
                "--field-id",
                field,
                "--file",
                safe_path,
            ]
        )

    def remove_attachments(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        field: str,
        file_tokens: list[str],
    ) -> dict[str, Any]:
        if not file_tokens:
            return {"removed": 0}
        args = [
            "base",
            "+record-remove-attachment",
            "--base-token",
            app_token,
            "--table-id",
            table_id,
            "--record-id",
            record_id,
            "--field-id",
            field,
        ]
        for token in file_tokens:
            args += ["--file-token", token]
        args += ["--yes"]
        return self._run(args)

    @staticmethod
    def _safe_upload_path(path: str, cwd: Path | None = None) -> str:
        """Return the project-relative path required by current lark-cli."""

        root = (cwd or Path.cwd()).resolve()
        resolved = Path(path).resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ExternalServiceError(
                f"Feishu upload path must stay inside project root {root}: {resolved}"
            ) from exc
        return f"./{relative.as_posix()}"

    def download_attachment(
        self,
        token: str,
        destination: Path,
        *,
        app_token: str | None = None,
        table_id: str | None = None,
        record_id: str | None = None,
    ) -> dict[str, Any]:
        if app_token and table_id and record_id:
            args = [
                "base",
                "+record-download-attachment",
                "--base-token",
                app_token,
                "--table-id",
                table_id,
                "--record-id",
                record_id,
                "--file-token",
                token,
                "--output",
                str(destination),
                "--overwrite",
            ]
        else:
            args = [
                "docs",
                "+media-download",
                "--token",
                token,
                "--output",
                str(destination),
                "--overwrite",
            ]
        result = self._run(args)
        path = Path(result.get("path", destination))
        return {
            "path": str(path),
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
        }


class FakeFeishuSyncClient:
    """In-memory deterministic sync client with write/read-back support."""

    DEFAULT_FIELDS = [
        "case编号",
        "case文件",
        "case评分",
        "case说明",
        "feed文件",
        "prompt文字",
        "prompt素材",
        "Xmax模型版本",
        "feed编号",
        "feed文件",
        "内容tag",
        "测试tag",
        "是否是测试数据",
        "prompt编号",
    ]

    def __init__(
        self,
        existing_records: dict[str, list[dict[str, Any]]] | None = None,
        declared_fields: list[str] | None = None,
    ) -> None:
        # table_id -> list of {"record_id", "fields"}
        self._tables: dict[str, list[dict[str, Any]]] = dict(existing_records or {})
        self._declared_fields = declared_fields or list(self.DEFAULT_FIELDS)
        self._next_record = 1
        self.upserts: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.field_checks: list[str] = []
        self.calls: list[str] = []
        self.fail_upsert: str | None = None
        self.fail_upload: str | None = None

    def get_fields(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        self.calls.append("get_fields")
        self.field_checks.append(table_id)
        return [{"field_name": name, "type": 1} for name in self._declared_fields]

    def list_records(
        self, app_token: str, table_id: str, page_token: str | None = None
    ) -> dict[str, Any]:
        self.calls.append("list_records")
        records = self._tables.get(table_id, [])
        if page_token is None:
            return {"records": records, "has_more": False, "page_token": None}
        return {"records": [], "has_more": False, "page_token": None}

    def get_bitable_records(
        self,
        app_token: str,
        table_id: str,
        page_token: str | None = None,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        """Alias used by the read-only case importer."""
        self.calls.append("get_bitable_records")
        return self.list_records(app_token, table_id, page_token=page_token)

    def upsert_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str | None,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append("upsert_record")
        if self.fail_upsert:
            raise ExternalServiceError(self.fail_upsert)
        records = self._tables.setdefault(table_id, [])
        if record_id:
            for record in records:
                if record["record_id"] == record_id:
                    record["fields"] = {**record["fields"], **fields}
                    self.upserts.append(
                        {"table_id": table_id, "record_id": record_id, "fields": fields}
                    )
                    return {"record_id": record_id, "fields": record["fields"]}
            raise ExternalServiceError(f"record not found for upsert: {record_id}")
        record_id = f"rec-{self._next_record}"
        self._next_record += 1
        record = {"record_id": record_id, "fields": dict(fields)}
        records.append(record)
        self.upserts.append({"table_id": table_id, "record_id": record_id, "fields": fields})
        return {"record_id": record_id, "fields": record["fields"]}

    def find_record(
        self,
        app_token: str,
        table_id: str,
        key_field: str,
        key_value: str,
        secondary_field: str,
        secondary_value: str,
        field_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        self.calls.append("find_record")
        for record in self._tables.get(table_id, []):
            fields = record.get("fields", {})
            if str(fields.get(key_field, "")) == str(key_value) and str(
                fields.get(secondary_field, "")
            ) == str(secondary_value):
                if field_names is None:
                    return record
                selected = dict.fromkeys([key_field, secondary_field, *field_names])
                return {
                    "record_id": record["record_id"],
                    "fields": {name: fields.get(name) for name in selected},
                }
        return None

    def upload_attachment(
        self, app_token: str, table_id: str, record_id: str, field: str, path: str
    ) -> dict[str, Any]:
        self.calls.append("upload_attachment")
        if self.fail_upload:
            raise ExternalServiceError(self.fail_upload)
        records = self._tables.get(table_id, [])
        for record in records:
            if record["record_id"] == record_id:
                token = f"tok-{file_sha256(path)[:8]}"
                record["fields"].setdefault(field, []).append(
                    {"file_token": token, "name": Path(path).name}
                )
                self.uploads.append(
                    {
                        "table_id": table_id,
                        "record_id": record_id,
                        "field": field,
                        "token": token,
                        "name": Path(path).name,
                        "path": path,
                    }
                )
                return {"token": token}
        raise ExternalServiceError(f"cannot upload to missing record {record_id}")

    def remove_attachments(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        field: str,
        file_tokens: list[str],
    ) -> dict[str, Any]:
        self.calls.append("remove_attachments")
        token_set = set(file_tokens)
        for record in self._tables.get(table_id, []):
            if record["record_id"] != record_id:
                continue
            current = record["fields"].get(field) or []
            record["fields"][field] = [
                item
                for item in current
                if (item.get("file_token") or item.get("token")) not in token_set
            ]
            return {"removed": len(current) - len(record["fields"][field])}
        raise ExternalServiceError(f"cannot remove attachment from missing record {record_id}")

    def download_attachment(self, token: str, destination: Path, **_: Any) -> dict[str, Any]:
        destination.write_bytes(b"attachment-bytes")
        return {
            "path": str(destination),
            "sha256": file_sha256(destination),
            "bytes": destination.stat().st_size,
        }
