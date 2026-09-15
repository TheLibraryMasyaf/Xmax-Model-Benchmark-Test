"""Decart Lucy 2.5 queue API adapter for offline video generation."""

from __future__ import annotations

import http.client
import json
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from ...errors import (
    AmbiguousSubmissionError,
    ExternalServiceError,
    MissingDependencyError,
)
from ...hashing import file_sha256
from ...time import utc_now
from .adapter import OfflineGenerationAdapter


# Decart documents a 200 MB upload limit. Keep a decimal-byte ceiling here so
# preflight and normalization enforce the same conservative boundary.
DECART_MAX_INPUT_BYTES = 200_000_000


class HttpDecartQueueTransport:
    """Minimal streaming multipart client; POST is never automatically retried."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.decart.ai/v1",
        api_key: str | None,
        timeout_s: int = 600,
        max_get_retries: int = 2,
    ) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid Decart API base URL")
        self._parsed = parsed
        self._base_path = parsed.path.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_get_retries = max_get_retries

    def preflight(self) -> dict[str, Any]:
        if not self._api_key:
            raise MissingDependencyError("DECART_API_KEY is required for Lucy 2.5 generation")
        return {
            "ok": True,
            "provider": "decart",
            "model": "lucy-2.5",
            "paid_request_sent": False,
        }

    def submit(
        self,
        *,
        video_path: str,
        prompt: str,
        reference_image_path: str | None = None,
        reference_image_mime_type: str | None = None,
        seed: int | None = None,
        resolution: str = "720p",
        enhance_prompt: bool = False,
        self_anchor: bool = True,
    ) -> dict[str, Any]:
        boundary = f"----xmax-test-{uuid.uuid4().hex}"
        fields: list[tuple[str, str]] = [
            ("prompt", prompt),
            ("resolution", resolution),
            ("enhance_prompt", str(enhance_prompt).lower()),
            ("self_anchor", str(self_anchor).lower()),
        ]
        if seed is not None:
            fields.append(("seed", str(seed)))
        files = [("data", Path(video_path), "video/mp4")]
        if reference_image_path:
            files.append(
                ("reference_image", Path(reference_image_path), reference_image_mime_type)
            )
        chunks = list(self._multipart_chunks(boundary, fields, files))
        content_length = sum(
            item.stat().st_size if isinstance(item, Path) else len(item) for item in chunks
        )
        connection = self._connection()
        request_started = False
        try:
            connection.putrequest("POST", self._path("/jobs/lucy-2.5"))
            connection.putheader("x-api-key", str(self._api_key or ""))
            connection.putheader("accept", "application/json")
            connection.putheader("content-type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("content-length", str(content_length))
            connection.endheaders()
            request_started = True
            for item in chunks:
                if isinstance(item, Path):
                    with item.open("rb") as handle:
                        while block := handle.read(1024 * 1024):
                            connection.send(block)
                else:
                    connection.send(item)
            response = connection.getresponse()
            body = response.read()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            if request_started:
                raise AmbiguousSubmissionError(
                    f"Decart create-job response was lost; do not resubmit automatically: {exc}"
                ) from exc
            raise ExternalServiceError(f"Decart create-job connection failed: {exc}") from exc
        finally:
            connection.close()
        # A successful POST with an unreadable or structurally unexpected body
        # is ambiguous: the server may have created a paid job even though the
        # client cannot recover its identifier. Never classify that as a safe
        # submission failure or issue a second POST automatically.
        try:
            data = self._decode_json(body, "create job")
        except ExternalServiceError as exc:
            if 200 <= response.status < 300:
                raise AmbiguousSubmissionError(
                    f"Decart accepted create-job but returned an unusable response: {exc}"
                ) from exc
            raise
        if response.status < 200 or response.status >= 300:
            raise ExternalServiceError(
                f"Decart create job failed with HTTP {response.status}: {self._message(data)}"
            )
        job_id = data.get("job_id") or data.get("id")
        if not job_id:
            raise AmbiguousSubmissionError("Decart accepted create-job but returned no job_id")
        return {"job_id": str(job_id), "status": data.get("status", "queued"), "raw": data}

    def poll(self, job_id: str) -> dict[str, Any]:
        data = self._get_json(f"/jobs/{job_id}")
        raw_status = str(data.get("status") or "unknown").lower()
        status = {
            "queued": "submitted",
            "pending": "submitted",
            "running": "processing",
            "processing": "processing",
            "completed": "completed",
            "succeeded": "completed",
            "failed": "error",
            "error": "error",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }.get(raw_status, raw_status)
        return {
            **data,
            "status": status,
            "provider_status": raw_status,
            "failure_class": data.get("failure_class") or data.get("error"),
        }

    def download(self, job_id: str, destination: Path) -> dict[str, Any]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}-{uuid.uuid4().hex}.partial")
        connection = self._connection()
        try:
            connection.request(
                "GET",
                self._path(f"/jobs/{job_id}/content"),
                headers={"x-api-key": str(self._api_key or ""), "accept": "video/mp4"},
            )
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                body = response.read()
                raise ExternalServiceError(
                    f"Decart result download failed with HTTP {response.status}: "
                    f"{body[:300].decode('utf-8', errors='replace')}"
                )
            with temp.open("wb") as handle:
                while block := response.read(1024 * 1024):
                    handle.write(block)
            if not temp.is_file() or temp.stat().st_size == 0:
                raise ExternalServiceError("Decart result download was empty")
            os.replace(temp, destination)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            if isinstance(exc, ExternalServiceError):
                raise
            raise ExternalServiceError(f"Decart result download failed: {exc}") from exc
        finally:
            connection.close()
            temp.unlink(missing_ok=True)
        return {
            "path": str(destination),
            "sha256": file_sha256(destination),
            "bytes": destination.stat().st_size,
        }

    def _get_json(self, suffix: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self._max_get_retries + 1):
            connection = self._connection()
            try:
                connection.request(
                    "GET",
                    self._path(suffix),
                    headers={"x-api-key": str(self._api_key or ""), "accept": "application/json"},
                )
                response = connection.getresponse()
                body = response.read()
                data = self._decode_json(body, suffix)
                if 200 <= response.status < 300:
                    return data
                if response.status not in {408, 425, 429, 500, 502, 503, 504}:
                    raise ExternalServiceError(
                        f"Decart GET {suffix} failed with HTTP {response.status}: "
                        f"{self._message(data)}"
                    )
                last_error = ExternalServiceError(
                    f"Decart GET {suffix} failed with HTTP {response.status}"
                )
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                connection.close()
            if attempt < self._max_get_retries:
                time.sleep(min(2**attempt, 4))
        raise ExternalServiceError(f"Decart GET {suffix} exhausted retries: {last_error}")

    def _connection(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._parsed.scheme == "https" else http.client.HTTPConnection
        return cls(self._parsed.hostname, self._parsed.port, timeout=self._timeout_s)

    def _path(self, suffix: str) -> str:
        return f"{self._base_path}{suffix}"

    @staticmethod
    def _decode_json(body: bytes, operation: str) -> dict[str, Any]:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExternalServiceError(f"Decart {operation} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ExternalServiceError(f"Decart {operation} returned non-object JSON")
        return data

    @staticmethod
    def _message(data: dict[str, Any]) -> str:
        return str(data.get("message") or data.get("error") or data)[:500]

    @staticmethod
    def _multipart_chunks(
        boundary: str,
        fields: list[tuple[str, str]],
        files: list[tuple[str, Path, str | None]],
    ) -> Iterable[bytes | Path]:
        marker = boundary.encode("ascii")
        for name, value in fields:
            yield b"--" + marker + b"\r\n"
            yield f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
            yield value.encode("utf-8") + b"\r\n"
        for name, path, explicit_mime in files:
            if not path.is_file():
                raise ExternalServiceError(f"Decart upload file missing: {path}")
            mime = (
                explicit_mime
                or mimetypes.guess_type(path.name)[0]
                or HttpDecartQueueTransport._magic_mime(path)
            )
            yield b"--" + marker + b"\r\n"
            yield (
                f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
            yield path
            yield b"\r\n"
        yield b"--" + marker + b"--\r\n"

    @staticmethod
    def _magic_mime(path: Path) -> str:
        header = path.read_bytes()[:16]
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "image/webp"
        return "application/octet-stream"


class FakeDecartQueueTransport:
    """Deterministic, zero-network Lucy transport for tests and dry integration."""

    def __init__(
        self,
        *,
        states: list[dict[str, Any]] | None = None,
        result_bytes: bytes = b"fake-decart-video",
        submit_error: Exception | None = None,
    ) -> None:
        self.states = list(states or [{"status": "completed"}])
        self.result_bytes = result_bytes
        self.submit_error = submit_error
        self.submissions: list[dict[str, Any]] = []
        self.poll_count = 0

    def preflight(self) -> dict[str, Any]:
        return {"ok": True, "provider": "decart", "fake": True}

    def submit(self, **payload: Any) -> dict[str, Any]:
        self.submissions.append(payload)
        if self.submit_error:
            raise self.submit_error
        return {"job_id": f"job-{len(self.submissions)}", "status": "queued", "raw": {}}

    def poll(self, job_id: str) -> dict[str, Any]:
        state = self.states[min(self.poll_count, len(self.states) - 1)]
        self.poll_count += 1
        return {**state, "job_id": job_id}

    def download(self, job_id: str, destination: Path) -> dict[str, Any]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.result_bytes)
        return {
            "path": str(destination),
            "sha256": file_sha256(destination),
            "bytes": destination.stat().st_size,
        }


class DecartOfflineGenerationAdapter(OfflineGenerationAdapter):
    """Maps frozen operation-recipe roles to Lucy 2.5 multipart fields."""

    def __init__(self, *args: Any, transport: Any, **kwargs: Any) -> None:
        super().__init__(
            *args,
            backend="rest",
            transport=transport,
            result_source=None,
            origin="decart_offline",
            provider_id="decart",
            **kwargs,
        )

    def submit(self, prepared: dict[str, Any]) -> dict[str, Any]:
        case = prepared["case"]
        assets = prepared["assets"]
        config = case.get("generation_config", {})
        video_path = Path(assets["ref_video"]["path"])
        if video_path.stat().st_size >= DECART_MAX_INPUT_BYTES:
            raise ExternalServiceError(
                f"Lucy input must be smaller than {DECART_MAX_INPUT_BYTES} bytes: "
                f"{video_path.stat().st_size} bytes"
            )
        response = self._transport.submit(
            video_path=str(video_path),
            reference_image_path=(assets.get("ref_image") or {}).get("path"),
            reference_image_mime_type=(assets.get("ref_image") or {}).get("mime_type"),
            prompt=case.get("prompt_text", ""),
            seed=config.get("seed"),
            resolution=config.get("resolution", "720p"),
            enhance_prompt=bool(config.get("enhance_prompt", False)),
            self_anchor=bool(config.get("self_anchor", True)),
        )
        job_id = response["job_id"]
        self._repository.append_event(
            prepared["run_id"],
            "task_submitted",
            payload={
                "external_task_id": job_id,
                "provider_status": response.get("status"),
                "provider_response": response.get("raw", {}),
            },
            external_key=job_id,
        )
        return {"external_task_id": job_id, "backend": "rest", "status": "submitted"}

    def poll(self, task: dict[str, Any]) -> dict[str, Any]:
        state = self._transport.poll(task["external_task_id"])
        return {
            "event": f"status_{state.get('status')}",
            "state": state,
            "terminal": state.get("status") in {"completed", "error", "cancelled"},
        }

    def collect(self, task: dict[str, Any]) -> dict[str, Any]:
        state = task.get("state", {})
        status = state.get("status", "error")
        return {
            "run_id": task.get("run_id", ""),
            "status": status,
            "failure_class": None if status == "completed" else "model_error",
            "metrics": {
                "external_task_id": task.get("external_task_id"),
                "backend": "decart_queue",
                "provider": "decart",
                "submitted_at": utc_now(),
                "provider_status": state.get("provider_status") or state.get("status"),
                "provider_error": state.get("error"),
            },
            "result_url": task.get("external_task_id") if status == "completed" else None,
        }

    def _register_result(self, case: dict[str, Any], job_id: str | None) -> tuple[str, float]:
        if not job_id:
            raise ExternalServiceError("completed Decart job has no job_id")
        target = self._artifacts.resolve(f"artifact://runs/{case['case_id']}/result.mp4")
        started = time.monotonic()
        download = self._transport.download(job_id, target)
        elapsed = round(time.monotonic() - started, 3)
        media = self._validator.validate(target, "result_video")
        return self._register_asset(download, media, case["case_id"]), elapsed
