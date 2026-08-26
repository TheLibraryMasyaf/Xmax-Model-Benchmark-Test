"""Feishu client adapters.

Real reads/writes go through ``lark-cli --as user``; the fake client makes the
Sheet/Base/Wiki sources testable offline. Every real call records a
desensitized request/response snapshot with the external ID.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Protocol

from ...errors import ExternalServiceError, MissingDependencyError
from ...feishu.record_pages import normalize_record_page
from ...hashing import file_sha256
from ...time import utc_now


class FeishuClient(Protocol):
    def get_sheet_meta(
        self, spreadsheet_token: str, sheet_id: str, cell_range: str = "A1:ZZ100000"
    ) -> dict[str, Any]:
        """Return range metadata plus cell values.

        Metadata must include ``has_more``, ``truncated``, ``actual_range``,
        ``row_indices`` and ``col_indices`` so sources can detect truncation.
        """

    def get_bitable_records(
        self,
        app_token: str,
        table_id: str,
        page_token: str | None = None,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of records plus ``has_more`` and next page_token."""

    def get_wiki_node(self, wiki_token: str) -> dict[str, Any]:
        """Resolve a wiki node to its real object type and token."""

    def download_attachment(self, token: str, destination: Path) -> dict[str, Any]:
        """Download one attachment and return path/sha256/bytes."""


class LarkCliFeishuClient:
    """Real client wrapping the lark-cli binary (default ``--as user``)."""

    provider = "lark-cli"

    def __init__(
        self,
        *,
        binary: str = "lark-cli",
        identity: str = "user",
        log_dir: Path | None = None,
        timeout: int = 120,
        max_retries: int = 2,
    ) -> None:
        self._binary = binary
        self._identity = identity
        self._log_dir = log_dir
        self._timeout = timeout
        self._max_retries = max_retries

    def _run(self, args: list[str]) -> dict[str, Any]:
        from shutil import which

        if which(self._binary) is None:
            raise MissingDependencyError(
                f"lark-cli binary not found: {self._binary}",
                entity_id="lark-cli",
            )
        command = [self._binary] + args + ["--as", self._identity, "--format", "json"]
        attempt = 0
        while True:
            attempt += 1
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=self._timeout
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._log(args, {"error": str(exc), "attempt": attempt})
                if attempt > self._max_retries:
                    raise ExternalServiceError(
                        f"lark-cli call failed after {attempt} attempts: {exc}"
                    ) from exc
                continue
            self._log(args, {"exit": result.returncode, "stdout": result.stdout[-4000:]})
            if result.returncode != 0:
                if attempt > self._max_retries:
                    raise ExternalServiceError(
                        f"lark-cli exited {result.returncode}: {result.stderr}"
                    )
                continue
            try:
                payload = json.loads(result.stdout)
                if isinstance(payload, dict) and payload.get("ok") is False:
                    raise ExternalServiceError(f"lark-cli failure: {payload.get('error')}")
                return payload.get("data", payload) if isinstance(payload, dict) else payload
            except json.JSONDecodeError as exc:
                if attempt > self._max_retries:
                    raise ExternalServiceError(f"lark-cli returned non-JSON: {exc}") from exc

    def _log(self, args: list[str], payload: dict[str, Any]) -> None:
        if self._log_dir is None:
            return
        self._log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"time": utc_now(), "args": args, "payload": payload}
        with (self._log_dir / "lark-cli-events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_sheet_meta(
        self, spreadsheet_token: str, sheet_id: str, cell_range: str = "A1:ZZ100000"
    ) -> dict[str, Any]:
        data = self._run(
            [
                "sheets",
                "+cells-get",
                "--spreadsheet-token",
                spreadsheet_token,
                "--sheet-id",
                sheet_id,
                "--range",
                cell_range,
            ]
        )
        values = (
            data.get("values") or data.get("rows") or data.get("valueRange", {}).get("values") or []
        )
        return {
            "has_more": bool(data.get("has_more", False)),
            "truncated": bool(data.get("truncated", False)),
            "actual_range": data.get("range") or data.get("actual_range"),
            "row_indices": list(range(1, len(values) + 1)),
            "col_indices": list(range(max((len(row or []) for row in values), default=0))),
            "rows": values,
        }

    def get_bitable_records(
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

    def get_wiki_node(self, wiki_token: str) -> dict[str, Any]:
        return self._run(["wiki", "+node-get", "--node-token", wiki_token])

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
        if not path.is_file():
            raise ExternalServiceError(f"attachment download produced no file: {token}")
        return {
            "path": str(path),
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
            "attachment_token": token,
        }


class FakeFeishuClient:
    """Deterministic fake with scriptable pages, truncation and failures."""

    provider = "fake"

    def __init__(
        self,
        *,
        sheet_meta: dict[str, Any] | None = None,
        bitable_pages: list[dict[str, Any]] | None = None,
        wiki_nodes: dict[str, dict[str, Any]] | None = None,
        attachments: dict[str, bytes] | None = None,
        fail_calls: set[str] | None = None,
    ) -> None:
        self._sheet_meta = sheet_meta or {
            "has_more": False,
            "truncated": False,
            "actual_range": "A1:C4",
            "row_indices": [1, 2, 3],
            "col_indices": [0, 1, 2],
            "rows": [],
        }
        self._bitable_pages = bitable_pages or [
            {"records": [], "has_more": False, "page_token": None}
        ]
        self._wiki_nodes = wiki_nodes or {}
        self._attachments = attachments or {}
        self._fail_calls = fail_calls or set()
        self.calls: list[str] = []

    def _maybe_fail(self, call: str) -> None:
        self.calls.append(call)
        if call in self._fail_calls:
            raise ExternalServiceError(f"fake feishu failure for {call}")

    def get_sheet_meta(
        self, spreadsheet_token: str, sheet_id: str, cell_range: str = "A1:ZZ100000"
    ) -> dict[str, Any]:
        self._maybe_fail("sheet_meta")
        return self._sheet_meta

    def get_bitable_records(
        self,
        app_token: str,
        table_id: str,
        page_token: str | None = None,
        view_id: str | None = None,
    ) -> dict[str, Any]:
        self._maybe_fail("bitable_records")
        for page in self._bitable_pages:
            if page.get("page_id") == page_token:
                return page
        if page_token is None:
            return self._bitable_pages[0]
        return self._bitable_pages[-1]

    def get_wiki_node(self, wiki_token: str) -> dict[str, Any]:
        self._maybe_fail("wiki_node")
        try:
            return self._wiki_nodes[wiki_token]
        except KeyError as exc:
            raise ExternalServiceError(f"unknown wiki node: {wiki_token}") from exc

    def download_attachment(self, token: str, destination: Path, **_: Any) -> dict[str, Any]:
        self._maybe_fail("download_attachment")
        try:
            data = self._attachments[token]
        except KeyError as exc:
            raise ExternalServiceError(f"unknown attachment token: {token}") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return {
            "path": str(destination),
            "sha256": file_sha256(destination),
            "bytes": len(data),
            "attachment_token": token,
        }
