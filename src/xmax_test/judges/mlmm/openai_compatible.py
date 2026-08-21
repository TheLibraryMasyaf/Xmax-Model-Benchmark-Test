"""OpenAI-compatible Chat Completions MLLM provider.

The adapter intentionally owns only the provider transport. Benchmark prompts,
identity fields and score fusion remain in the local Judge layer.
"""

from __future__ import annotations

import base64
import csv
import json
import mimetypes
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ...errors import ConfigError, ExternalServiceError
from .base import MlmmResponse


class OpenAiCompatibleProvider:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        model: str | None = None,
        models: list[str] | None = None,
        api_key_env: str | None = None,
        credential_csv: str | Path | None = None,
        api_key_csv_field: str = "apiKey",
        endpoint_csv_field: str = "openAiCompatible",
        timeout_s: int = 300,
        extra_headers: dict[str, str] | None = None,
        response_format_type: str = "json_schema",
        request_options: dict[str, Any] | None = None,
        direct_media: bool = False,
        video_options: dict[str, Any] | None = None,
        image_options: dict[str, Any] | None = None,
        max_base64_bytes: int = 10_000_000,
    ) -> None:
        credentials = _read_key_value_csv(credential_csv) if credential_csv else {}
        self._endpoint = _chat_completions_endpoint(
            endpoint or credentials.get(endpoint_csv_field, "")
        )
        configured_models = list(models or ([model] if model else []))
        if not configured_models:
            raise ConfigError("OpenAI-compatible MLLM provider requires model(s)")
        self._models = configured_models
        self._model_index = 0
        self._model_lock = threading.Lock()
        self._api_key_env = api_key_env
        self._api_key = credentials.get(api_key_csv_field)
        self._timeout = timeout_s
        self._extra_headers = dict(extra_headers or {})
        self._response_format_type = response_format_type
        self._request_options = dict(request_options or {})
        self._direct_media = bool(direct_media)
        self._video_options = dict(video_options or {})
        self._image_options = dict(image_options or {})
        self._max_base64_bytes = int(max_base64_bytes)

    @property
    def provider_id(self) -> str:
        return "openai_compatible"

    def complete_json(
        self,
        *,
        prompt: str,
        image_paths: list[str],
        output_schema: dict[str, Any],
        media_inputs: list[dict[str, Any]] | None = None,
    ) -> MlmmResponse:
        api_key = os.getenv(self._api_key_env) if self._api_key_env else self._api_key
        if not api_key:
            raise ConfigError(
                "MLLM API key is missing from the configured environment or credential CSV"
            )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        input_mode = "direct_media" if self._direct_media and media_inputs else "frames"
        if input_mode == "direct_media":
            content.extend(self._direct_media_content(media_inputs or []))
        else:
            for image_path in image_paths:
                content.append(self._image_content(Path(image_path)))
        if self._response_format_type == "json_object":
            content[0]["text"] = (
                "请严格仅输出符合下列 JSON Schema 的 JSON 对象。\n"
                f"JSON Schema: {json.dumps(output_schema, ensure_ascii=False)}\n"
                + content[0]["text"]
            )
            response_format: dict[str, Any] = {"type": "json_object"}
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "xmax_mlmm_output",
                    "strict": True,
                    "schema": output_schema,
                },
            }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        attempted: list[str] = []
        while True:
            model_index, current_model = self._current_model()
            attempted.append(current_model)
            body = {
                "model": current_model,
                "messages": [{"role": "user", "content": content}],
                "response_format": response_format,
                **self._request_options,
            }
            request = urllib.request.Request(
                self._endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    raw_text = response.read().decode("utf-8")
                if _is_free_tier_exhausted(200, raw_text):
                    if self._advance_model(model_index):
                        continue
                    raise ExternalServiceError(
                        "all configured MLLM models exhausted their free tier"
                    )
                break
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                if _is_free_tier_exhausted(exc.code, error_body):
                    if self._advance_model(model_index):
                        continue
                    raise ExternalServiceError(
                        "all configured MLLM models exhausted their free tier"
                    ) from exc
                raise ExternalServiceError(
                    f"MLLM API request failed with HTTP {exc.code}: "
                    f"{_safe_error_summary(error_body)}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ExternalServiceError(f"MLLM API request failed: {exc}") from exc
        try:
            envelope = json.loads(raw_text)
            message = envelope["choices"][0]["message"]["content"]
            payload = json.loads(message) if isinstance(message, str) else message
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ExternalServiceError(
                f"MLLM API returned an incompatible response: {exc}"
            ) from exc
        return MlmmResponse(
            payload=payload,
            raw_text=raw_text,
            provider_id=self.provider_id,
            model=current_model,
            usage=envelope.get("usage", {}),
            metadata={
                "model_fallback_attempts": attempted,
                "input_mode": input_mode,
                "media_roles": [
                    item.get("role", "unknown") for item in (media_inputs or [])
                ] if input_mode == "direct_media" else [],
            },
        )

    def _direct_media_content(
        self, media_inputs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for item in media_inputs:
            role = str(item.get("role") or "unknown")
            kind = str(item.get("kind") or "")
            content.append(
                {"type": "text", "text": f"[INPUT_ROLE:{role}]"}
            )
            if kind == "text":
                content.append(
                    {"type": "text", "text": str(item.get("text") or "")}
                )
                continue
            url = item.get("url")
            path_value = item.get("path")
            if not url and not path_value:
                raise ConfigError(f"MLLM media input {role} has neither path nor url")
            if path_value and not url:
                path = Path(str(path_value))
                mime = _media_mime(path, kind)
                url = self._data_url(path, mime)
            if kind == "video":
                content.append(
                    {
                        "type": "video_url",
                        "video_url": {
                            "url": str(url),
                            **{
                                key: value
                                for key, value in self._video_options.items()
                                if key == "fps"
                            },
                        },
                        **{
                            key: value
                            for key, value in self._video_options.items()
                            if key != "fps"
                        },
                    }
                )
            elif kind == "image":
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": str(url)},
                        **self._image_options,
                    }
                )
            else:
                raise ConfigError(f"unsupported MLLM media kind for {role}: {kind}")
        return content

    def _image_content(self, path: Path) -> dict[str, Any]:
        mime = _media_mime(path, "image")
        return {
            "type": "image_url",
            "image_url": {"url": self._data_url(path, mime)},
        }

    def _data_url(self, path: Path, mime: str) -> str:
        if not path.is_file():
            raise ConfigError(f"MLLM media file does not exist: {path}")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        if len(encoded) > self._max_base64_bytes:
            raise ConfigError(
                f"MLLM media item exceeds Base64 limit ({len(encoded)} > "
                f"{self._max_base64_bytes} bytes): {path}; provide a provider-accessible URL"
            )
        return f"data:{mime};base64,{encoded}"

    def _current_model(self) -> tuple[int, str]:
        with self._model_lock:
            return self._model_index, self._models[self._model_index]

    def _advance_model(self, exhausted_index: int) -> bool:
        with self._model_lock:
            if self._model_index == exhausted_index:
                if self._model_index + 1 >= len(self._models):
                    return False
                self._model_index += 1
            return True


def _read_key_value_csv(path: str | Path) -> dict[str, str]:
    csv_path = Path(path)
    if not csv_path.is_file():
        raise ConfigError(f"MLLM credential CSV does not exist: {csv_path}")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    return {
        row[0].strip(): row[1].strip()
        for row in rows
        if len(row) >= 2 and row[0].strip() and row[1].strip()
    }


def _chat_completions_endpoint(value: str) -> str:
    endpoint = value.rstrip("/")
    if not endpoint:
        raise ConfigError("OpenAI-compatible MLLM endpoint is missing")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    return endpoint


def _is_free_tier_exhausted(status: int, body: str) -> bool:
    del status  # Alibaba's structured error code is authoritative across gateways.
    lowered = body.lower()
    if "allocationquota.freetieronly" in lowered:
        return True
    if "free tier of the model has been exhausted" in lowered:
        return True
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    sources = [payload, error] if isinstance(error, dict) else [payload]
    return any(
        str(source.get("code") or "").lower()
        == "allocationquota.freetieronly"
        for source in sources
    )


def _safe_error_summary(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    source = error if isinstance(error, dict) else payload
    if not isinstance(source, dict):
        return "incompatible error response"
    code = source.get("code") or source.get("type") or "unknown"
    message = source.get("message") or ""
    return f"{code}: {message}"[:300]


def _media_mime(path: Path, kind: str) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed and guessed.startswith(f"{kind}/"):
        return guessed
    head = path.read_bytes()[:32]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    return "video/mp4" if kind == "video" else "image/jpeg"
