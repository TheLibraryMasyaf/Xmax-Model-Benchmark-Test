"""Offline-task REST adapter.

Flow: validate local assets -> acquire upload credentials -> upload & cache
the official XMAX media URL -> POST /offline-task -> record task uid -> poll
submitted/processing/completed/error -> download result -> validate media ->
save cost and billing facts.
"""

from __future__ import annotations

import json
import mimetypes
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from ...errors import ExternalServiceError, MissingDependencyError
from ...hashing import file_sha256


class OfflineTaskTransport(Protocol):
    def preflight(self) -> dict[str, Any]: ...

    def upload_credentials(self) -> dict[str, Any]: ...

    def upload_image(
        self,
        local_path: str,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]: ...

    def upload_video(
        self,
        local_path: str,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]: ...

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def poll(self, task_uid: str) -> dict[str, Any]: ...

    def download(self, url: str, destination: Path) -> dict[str, Any]: ...


class HttpOfflineTaskTransport:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: int = 120,
        max_retries: int = 2,
        quality: str = "hd",
        fps: int | None = None,
    ) -> None:
        if quality not in {"sd", "hd"}:
            raise ValueError("offline quality must be 'sd' or 'hd'")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._quality = quality
        self._fps = fps
        self._sts: dict[str, Any] | None = None

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        for attempt in range(1, self._max_retries + 2):
            request = urllib.request.Request(
                f"{self._base_url}{path}", data=data, method=method, headers=headers
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                if not isinstance(raw, dict):
                    raise ExternalServiceError(f"offline task HTTP {path} returned non-object JSON")
                if raw.get("success") is False:
                    raise ExternalServiceError(
                        f"offline task API {path} failed: code={raw.get('code')} "
                        f"message={raw.get('message', '')}"
                    )
                data_value = raw.get("data", raw)
                if not isinstance(data_value, dict):
                    raise ExternalServiceError(f"offline task API {path} returned invalid data")
                return data_value
            except urllib.error.HTTPError as exc:
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
                if not retryable or attempt > self._max_retries:
                    raise ExternalServiceError(
                        f"offline task HTTP {path} failed with {exc.code}"
                    ) from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                json.JSONDecodeError,
            ) as exc:
                if attempt > self._max_retries:
                    raise ExternalServiceError(f"offline task HTTP {path} failed: {exc}") from exc
            time.sleep(min(2 ** (attempt - 1), 4))
        raise AssertionError("unreachable")

    def upload_credentials(self) -> dict[str, Any]:
        self._sts = self._request("GET", "/cos/sts")
        return self._sts

    def preflight(self) -> dict[str, Any]:
        """Validate the real upload transport before creating paid Run rows."""

        try:
            from qcloud_cos import CosConfig, CosS3Client  # noqa: F401
        except ImportError as exc:
            raise MissingDependencyError(
                "real XMAX upload requires importable CosConfig and CosS3Client; "
                "install with: pip install -e '.[production]'"
            ) from exc
        sts = self.upload_credentials()
        credentials = sts.get("credentials", {})
        missing = [key for key in ("bucket", "region", "prefix") if not sts.get(key)]
        missing_credentials = [
            key
            for key in ("accessKeyId", "secretAccessKey", "sessionToken")
            if not credentials.get(key)
        ]
        if missing or missing_credentials:
            raise ExternalServiceError(
                "XMAX COS STS preflight is incomplete: "
                f"missing={missing}, missing_credentials={missing_credentials}"
            )
        return {
            "ok": True,
            "bucket": sts["bucket"],
            "region": sts["region"],
            "prefix_present": bool(sts["prefix"]),
        }

    @staticmethod
    def _detect_media(local_path: str, expected_prefix: str) -> tuple[str, str]:
        """Return an SDK-compatible MIME type and extension for extensionless artifacts."""

        path = Path(local_path)
        guessed = mimetypes.guess_type(path.name)[0]
        if guessed and guessed.startswith(expected_prefix):
            extension = path.suffix.lower().lstrip(".")
            return guessed, extension or ("mp4" if expected_prefix == "video/" else "jpg")

        header = path.read_bytes()[:32]
        if expected_prefix == "image/":
            if header.startswith(b"\x89PNG\r\n\x1a\n"):
                return "image/png", "png"
            if header.startswith(b"\xff\xd8\xff"):
                return "image/jpeg", "jpg"
            if header.startswith((b"GIF87a", b"GIF89a")):
                return "image/gif", "gif"
            if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
                return "image/webp", "webp"
        else:
            if len(header) >= 12 and header[4:8] == b"ftyp":
                if header[8:12] == b"qt  ":
                    return "video/quicktime", "mov"
                return "video/mp4", "mp4"
            if header.startswith(b"\x1aE\xdf\xa3"):
                return "video/webm", "webm"
            if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
                return "video/x-msvideo", "avi"
        raise ExternalServiceError(
            f"cannot determine official XMAX {expected_prefix.rstrip('/')} upload type: {path}"
        )

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        # Mirrors the SDK's safe-name contract while keeping Python uploads ASCII-safe.
        safe = re.sub(r"[^\w.\-\u4e00-\u9fa5]", "_", filename.strip(), flags=re.UNICODE)
        return re.sub(r"_+", "_", safe) or "upload"

    @staticmethod
    def _resolve_cos_upload_url(raw: Any, sts: dict[str, Any], key: str) -> str:
        """Mirror @xmaxai/sdk resolveCosUploadUrl instead of inventing a public URL."""

        location = raw.get("Location") if isinstance(raw, dict) else None
        if isinstance(location, str) and location.strip():
            location = location.strip()
            return (
                location
                if re.match(r"^https?://", location, re.IGNORECASE)
                else f"https://{location.lstrip('/')}"
            )

        endpoint = str(sts.get("endpoint") or "").strip()
        encoded_key = urllib.parse.quote(key.lstrip("/"), safe="/")
        if endpoint:
            endpoint_url = (
                endpoint
                if re.match(r"^https?://", endpoint, re.IGNORECASE)
                else f"https://{endpoint.lstrip('/')}"
            )
            parsed = urllib.parse.urlsplit(endpoint_url)
            hostname = parsed.hostname or ""
            bucket = str(sts["bucket"])
            if hostname.startswith("cos.") and not hostname.startswith(f"{bucket}."):
                hostname = f"{bucket}.{hostname}"
                if parsed.port:
                    hostname = f"{hostname}:{parsed.port}"
            base_path = parsed.path.rstrip("/")
            return urllib.parse.urlunsplit(
                (
                    parsed.scheme or "https",
                    hostname,
                    f"{base_path}/{encoded_key}",
                    "",
                    "",
                )
            )
        return f"https://{sts['bucket']}.cos.{sts['region']}.myqcloud.com/{encoded_key}"

    def _upload_object(
        self,
        local_path: str,
        expected_prefix: str,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        # STS temporary credentials have a short lifetime; never reuse a stale
        # cached credential across long-running batches.  Refresh before every
        # upload so an expired AccessKeyId cannot fail the whole queue.
        sts = self.upload_credentials()
        credentials = sts.get("credentials", {})
        required = ("bucket", "region", "prefix")
        if any(not sts.get(key) for key in required) or not credentials:
            raise ExternalServiceError("XMAX COS STS response is incomplete")
        detected_mime, extension = self._detect_media(local_path, expected_prefix)
        resolved_mime = (mime_type or "").strip() or detected_mime
        if not resolved_mime.startswith(expected_prefix):
            raise ExternalServiceError(
                f"official XMAX upload requires {expected_prefix} MIME, got {resolved_mime!r}"
            )
        source_name = filename or Path(local_path).name
        if not Path(source_name).suffix or Path(source_name).suffix.lower() == ".bin":
            source_name = f"{Path(source_name).stem or 'upload'}.{extension}"
        safe_file_name = self._sanitize_filename(source_name)
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as exc:
            raise MissingDependencyError(
                "real XMAX upload requires optional dependency cos-python-sdk-v5; "
                "install with: pip install -e '.[production]'"
            ) from exc
        config = CosConfig(
            Region=sts["region"],
            SecretId=credentials.get("accessKeyId"),
            SecretKey=credentials.get("secretAccessKey"),
            Token=credentials.get("sessionToken"),
        )
        # This is the same object-key contract used by @xmaxai/sdk uploadObject().
        key = f"{sts['prefix']}{int(time.time() * 1000)}_{safe_file_name}"
        client = CosS3Client(config)
        with Path(local_path).open("rb") as handle:
            raw = client.put_object(
                Bucket=sts["bucket"],
                Body=handle,
                Key=key,
                ContentType=resolved_mime,
            )
        official_url = self._resolve_cos_upload_url(raw, sts, key)
        return {
            "url": official_url,
            "sha256": file_sha256(local_path),
            "key": key,
            "mime_type": resolved_mime,
            "raw": raw,
            "sts_endpoint": sts.get("endpoint"),
        }

    def upload_image(
        self,
        local_path: str,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        return self._upload_object(local_path, "image/", filename=filename, mime_type=mime_type)

    def upload_video(
        self,
        local_path: str,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        return self._upload_object(local_path, "video/", filename=filename, mime_type=mime_type)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        # The domain adapter also carries local audit fields (taskUid, model,
        # audio baseline). Project only the fields accepted by the public API
        # so an internal contract extension cannot break a paid submission.
        request_payload = {
            "prompt": payload.get("prompt", ""),
            "refVideoPath": payload.get("refVideoPath"),
            "quality": self._quality,
        }
        if payload.get("refImagePath"):
            request_payload["refImagePath"] = payload["refImagePath"]
        if self._fps is not None:
            request_payload["fps"] = self._fps
        data = self._request("POST", "/offline-task", request_payload)
        if "uid" in data and "taskUid" not in data:
            data["taskUid"] = data["uid"]
        return data

    def poll(self, task_uid: str) -> dict[str, Any]:
        data = self._request("GET", f"/offline-task/{task_uid}")
        result = data.get("result") or {}
        if isinstance(result, dict) and result.get("result_url"):
            data.setdefault("result_url", result["result_url"])
        if "chargePoints" in data:
            data.setdefault("credits", data["chargePoints"])
        if "billableDurationSeconds" in data:
            data.setdefault("billed_seconds", data["billableDurationSeconds"])
        return data

    def download(self, url: str, destination: Path) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as response:
                destination.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExternalServiceError(f"result download failed: {exc}") from exc
        return {"sha256": file_sha256(destination), "bytes": destination.stat().st_size}


class FakeOfflineTaskTransport:
    """Deterministic offline-task fake with scriptable states."""

    def __init__(
        self,
        poll_states: list[dict[str, Any]] | None = None,
        *,
        submit_error: str | None = None,
        download_error: str | None = None,
        result_bytes: bytes = b"fake-result-video",
    ) -> None:
        self._poll_states = poll_states or [
            {"status": "submitted"},
            {"status": "processing"},
            {
                "status": "completed",
                "result_url": "https://example.invalid/result.mp4",
                "credits": 100,
                "billed_seconds": 12,
            },
        ]
        self._submit_error = submit_error
        self._download_error = download_error
        self._result_bytes = result_bytes
        self._poll_index = 0
        self._submit_count = 0
        self.calls: list[str] = []
        self.submitted_payloads: list[dict[str, Any]] = []

    def upload_credentials(self) -> dict[str, Any]:
        self.calls.append("upload_credentials")
        return {"url": "https://upload.example.invalid/", "key": "cos-key"}

    def _upload(
        self, local_path: str, expected_prefix: str, filename: str | None
    ) -> dict[str, Any]:
        self.calls.append(f"upload_{expected_prefix.rstrip('/')}")
        fake_key = Path(filename).stem if filename else file_sha256(local_path)
        return {
            "url": f"https://assets.example.invalid/{fake_key}",
            "sha256": file_sha256(local_path),
        }

    def upload_image(
        self,
        local_path: str,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        return self._upload(local_path, "image/", filename)

    def upload_video(
        self,
        local_path: str,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        return self._upload(local_path, "video/", filename)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("submit")
        if self._submit_error:
            raise ExternalServiceError(self._submit_error)
        self.submitted_payloads.append(payload)
        self._submit_count += 1
        return {"taskUid": f"task-fake-{self._submit_count}", "status": "submitted"}

    def poll(self, task_uid: str) -> dict[str, Any]:
        self.calls.append("poll")
        state = self._poll_states[min(self._poll_index, len(self._poll_states) - 1)]
        self._poll_index += 1
        return {**state, "taskUid": task_uid}

    def download(self, url: str, destination: Path) -> dict[str, Any]:
        self.calls.append("download")
        if self._download_error:
            raise ExternalServiceError(self._download_error)
        destination.write_bytes(self._result_bytes)
        return {"sha256": file_sha256(destination), "bytes": destination.stat().st_size}
