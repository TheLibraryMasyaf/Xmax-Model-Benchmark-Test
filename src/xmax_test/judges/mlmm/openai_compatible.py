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
import random
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from ...errors import (
    ConfigError,
    EvaluationBudgetPausedError,
    EvaluationInfrastructurePausedError,
    ExternalServiceError,
    MlmmAuthenticationError,
    MlmmInvalidRequestError,
    MlmmQuotaSafetyError,
    MlmmRateLimitError,
    MlmmTimeoutError,
    MlmmTransportError,
)
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
        budget_gate: Any = None,
        transport_max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        retry_backoff_max_seconds: float = 8.0,
        retry_jitter_seconds: float = 0.25,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[float, float], float] = random.uniform,
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
        self._budget_gate = budget_gate
        self._transport_max_retries = int(transport_max_retries)
        self._retry_backoff_seconds = float(retry_backoff_seconds)
        self._retry_backoff_max_seconds = float(retry_backoff_max_seconds)
        self._retry_jitter_seconds = float(retry_jitter_seconds)
        self._sleep = sleep_fn
        self._random = random_fn
        if self._transport_max_retries < 0:
            raise ConfigError("transport_max_retries must be at least zero")
        if self._retry_backoff_seconds < 0 or self._retry_backoff_max_seconds < 0:
            raise ConfigError("MLLM retry backoff values cannot be negative")
        if self._retry_backoff_max_seconds < self._retry_backoff_seconds:
            raise ConfigError(
                "retry_backoff_max_seconds must be greater than or equal to retry_backoff_seconds"
            )
        if self._retry_jitter_seconds < 0:
            raise ConfigError("retry_jitter_seconds cannot be negative")
        self._paid_model = budget_gate.policy.model if budget_gate is not None else None
        if self._paid_model is not None:
            if configured_models.count(self._paid_model) != 1:
                raise ConfigError("paid_fallback.model must occur exactly once in provider.models")
            if configured_models[-1] != self._paid_model:
                raise ConfigError("paid_fallback.model must be the final provider.models entry")

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
        transport_attempts: list[dict[str, Any]] = []
        transport_failures: dict[str, int] = {}
        while True:
            model_index, current_model = self._current_model()
            if not attempted or attempted[-1] != current_model:
                attempted.append(current_model)
            transport_attempts.append(
                {
                    "model": current_model,
                    "attempt": transport_failures.get(current_model, 0) + 1,
                }
            )
            paid_reservation = None
            if current_model == self._paid_model:
                paid_reservation = self._budget_gate.reserve_paid_call()
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
                # urllib's per-request ``timeout`` covers connect and read, but
                # through a TUN/NAT proxy (e.g. macOS Surge/Clash virtual
                # interface) the underlying socket can lose its deadline after
                # the CONNECT handshake, letting response.read() block forever
                # and stall the whole evaluation pipeline.  Set the
                # process-wide socket default timeout as a hard backstop and
                # restore it as soon as the request completes.
                previous_socket_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(self._timeout)
                try:
                    with urllib.request.urlopen(
                        request, timeout=self._timeout
                    ) as response:
                        raw_text = response.read().decode("utf-8")
                finally:
                    socket.setdefaulttimeout(previous_socket_timeout)
                failure = _classify_provider_error(200, raw_text, {})
                if failure is not None:
                    if paid_reservation is not None:
                        self._budget_gate.release(
                            paid_reservation["reservation_id"],
                            reason=f"provider returned {failure.kind} in a success envelope",
                        )
                    self._handle_provider_failure(
                        failure,
                        model_index=model_index,
                        current_model=current_model,
                        paid_reservation=paid_reservation,
                        transport_failures=transport_failures,
                    )
                    continue
                break
            except urllib.error.HTTPError as exc:
                try:
                    error_body = exc.read().decode("utf-8", errors="replace")
                finally:
                    exc.close()
                failure = _classify_provider_error(exc.code, error_body, exc.headers or {})
                if paid_reservation is not None:
                    self._budget_gate.release(
                        paid_reservation["reservation_id"],
                        reason=f"provider rejected request with HTTP {exc.code}",
                    )
                try:
                    self._handle_provider_failure(
                        failure
                        or _ProviderFailure(
                            "provider_error",
                            _safe_error_summary(error_body),
                        ),
                        model_index=model_index,
                        current_model=current_model,
                        paid_reservation=paid_reservation,
                        transport_failures=transport_failures,
                    )
                except Exception as classified:
                    raise classified from exc
                continue
            except EvaluationBudgetPausedError:
                raise
            except (TimeoutError, socket.timeout) as exc:
                if paid_reservation is not None:
                    self._budget_gate.forfeit(
                        paid_reservation["reservation_id"],
                        reason="paid request timed out; billing outcome is ambiguous",
                    )
                elif self._retry_free_transport(
                    current_model,
                    retry_after=None,
                    transport_failures=transport_failures,
                ):
                    continue
                raise MlmmTimeoutError(
                    f"MLLM API request exceeded the {self._timeout}s timeout after "
                    f"{transport_failures.get(current_model, 0) + 1} attempts on "
                    f"model={current_model}: {exc}"
                ) from exc
            except urllib.error.URLError as exc:
                if paid_reservation is not None:
                    reason = getattr(exc, "reason", None)
                    if isinstance(reason, (TimeoutError, socket.timeout)):
                        self._budget_gate.forfeit(
                            paid_reservation["reservation_id"],
                            reason="paid request timed out; billing outcome is ambiguous",
                        )
                        raise MlmmTimeoutError(
                            f"MLLM API request exceeded the {self._timeout}s timeout: {exc}"
                        ) from exc
                    self._budget_gate.release(
                        paid_reservation["reservation_id"],
                        reason="network failure before a confirmed provider response",
                    )
                elif self._retry_free_transport(
                    current_model,
                    retry_after=None,
                    transport_failures=transport_failures,
                ):
                    continue
                raise MlmmTransportError(
                    "MLLM network failure exhausted same-model retries: "
                    f"model={current_model} error={exc}"
                ) from exc
            except OSError as exc:
                if paid_reservation is not None:
                    self._budget_gate.release(
                        paid_reservation["reservation_id"],
                        reason="local transport failure before a confirmed provider response",
                    )
                elif self._retry_free_transport(
                    current_model,
                    retry_after=None,
                    transport_failures=transport_failures,
                ):
                    continue
                raise MlmmTransportError(
                    "MLLM local transport failure exhausted same-model retries: "
                    f"model={current_model} error={exc}"
                ) from exc
        paid_call = paid_reservation is not None
        paid_reservation_finalized = False
        try:
            envelope = json.loads(raw_text)
            usage = envelope.get("usage", {})
            if paid_reservation is not None:
                if usage and (
                    usage.get("prompt_tokens") is not None
                    or usage.get("input_tokens") is not None
                ):
                    self._budget_gate.settle(paid_reservation["reservation_id"], usage)
                else:
                    self._budget_gate.forfeit(
                        paid_reservation["reservation_id"],
                        reason="paid response omitted billable token usage",
                    )
                paid_reservation_finalized = True
            message = envelope["choices"][0]["message"]["content"]
            payload = json.loads(message) if isinstance(message, str) else message
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            if paid_call:
                if paid_reservation is not None and not paid_reservation_finalized:
                    self._budget_gate.forfeit(
                        paid_reservation["reservation_id"],
                        reason="paid response was incompatible and billing outcome is ambiguous",
                    )
                raise EvaluationInfrastructurePausedError(
                    "paid MLLM response was incompatible after a billable request; "
                    "automatic paid retry is disabled"
                ) from exc
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
                "transport_attempts": transport_attempts,
                "paid_fallback": current_model == self._paid_model,
                "input_mode": input_mode,
                "media_roles": [item.get("role", "unknown") for item in (media_inputs or [])]
                if input_mode == "direct_media"
                else [],
            },
        )

    def _handle_provider_failure(
        self,
        failure: _ProviderFailure,
        *,
        model_index: int,
        current_model: str,
        paid_reservation: dict[str, Any] | None,
        transport_failures: dict[str, int],
    ) -> None:
        if failure.kind == "free_tier_exhausted":
            if current_model == self._paid_model:
                state = self._budget_gate.pause(
                    "paid fallback still has 'use free tier only' enabled in the provider console"
                )
                raise self._budget_gate._paused_error(state)
            if self._advance_model(model_index):
                return
            raise MlmmQuotaSafetyError(
                "all configured MLLM models exhausted their confirmed free tier; "
                "no untried fallback remains"
            )
        if failure.kind == "quota_review_required":
            if current_model == self._paid_model:
                state = self._budget_gate.pause(
                    "paid fallback returned an unrecognized quota/account-balance response: "
                    f"{failure.summary}"
                )
                raise self._budget_gate._paused_error(state)
            raise MlmmQuotaSafetyError(
                "MLLM evaluation paused on an unrecognized quota-like response; "
                "no model rotation or paid authorization was attempted: "
                f"model={current_model} error={failure.summary}"
            )
        if failure.kind == "authentication_failed":
            raise MlmmAuthenticationError(
                f"MLLM authentication/authorization failed for model={current_model}: "
                f"{failure.summary}"
            )
        if failure.kind == "invalid_request":
            raise MlmmInvalidRequestError(
                f"MLLM request was rejected for model={current_model}: {failure.summary}"
            )
        if failure.kind in {"rate_limited", "service_unavailable", "transport"}:
            # Paid attempts are never retried automatically: even an explicit
            # rejection can be difficult to reconcile against the provider's
            # external account ledger.  The reservation was already released
            # by the caller for an HTTP rejection.
            if paid_reservation is None and self._retry_free_transport(
                current_model,
                retry_after=failure.retry_after_seconds,
                transport_failures=transport_failures,
            ):
                return
            error_type = (
                MlmmRateLimitError
                if failure.kind == "rate_limited"
                else MlmmTransportError
            )
            raise error_type(
                f"MLLM {failure.kind} exhausted same-model retries: "
                f"model={current_model} error={failure.summary}"
            )
        raise ExternalServiceError(
            f"MLLM API returned an unclassified provider error for model={current_model}: "
            f"{failure.summary}"
        )

    def _retry_free_transport(
        self,
        model: str,
        *,
        retry_after: float | None,
        transport_failures: dict[str, int],
    ) -> bool:
        failure_count = int(transport_failures.get(model, 0))
        if model == self._paid_model or failure_count >= self._transport_max_retries:
            return False
        delay = self._retry_delay(failure_count, retry_after=retry_after)
        transport_failures[model] = failure_count + 1
        self._sleep(delay)
        return True

    def _retry_delay(self, failure_count: int, *, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(max(0.0, retry_after), self._retry_backoff_max_seconds)
        exponential = min(
            self._retry_backoff_seconds * (2**failure_count),
            self._retry_backoff_max_seconds,
        )
        return exponential + self._random(0.0, self._retry_jitter_seconds)

    def _direct_media_content(self, media_inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for item in media_inputs:
            role = str(item.get("role") or "unknown")
            kind = str(item.get("kind") or "")
            content.append({"type": "text", "text": f"[INPUT_ROLE:{role}]"})
            if kind == "text":
                content.append({"type": "text", "text": str(item.get("text") or "")})
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
                            key: value for key, value in self._video_options.items() if key != "fps"
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


@dataclass(frozen=True)
class _ProviderFailure:
    kind: str
    summary: str
    retry_after_seconds: float | None = None


def _classify_provider_error(
    status: int,
    body: str,
    headers: Mapping[str, Any],
) -> _ProviderFailure | None:
    """Normalize provider/gateway variants without granting billing authority.

    Only a confirmed free-tier signal may rotate models.  Other quota-like
    responses fail closed so an operator/monitor can inspect the new response
    before the rule corpus is extended.
    """

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    if status < 400 and not (isinstance(payload, dict) and payload.get("error")):
        return None

    sources: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        sources.append(payload)
        error = payload.get("error")
        if isinstance(error, dict):
            sources.insert(0, error)
    codes = " ".join(
        str(source.get(key) or "")
        for source in sources
        for key in ("code", "type")
    ).lower()
    messages = " ".join(str(source.get("message") or "") for source in sources).lower()
    lowered = f"{codes} {messages} {body.lower()}"
    summary = _safe_error_summary(body)

    confirmed_free_tier = (
        "allocationquota.freetieronly" in lowered
        or "free_quota_exhausted" in codes
        or "free tier of the model has been exhausted" in lowered
        or (
            any(phrase in lowered for phrase in ("free quota exhausted", "free quota is exhausted"))
            and "free tier only" in lowered
        )
    )
    if confirmed_free_tier:
        return _ProviderFailure("free_tier_exhausted", summary)

    if status == 429 or any(
        token in codes for token in ("throttl", "rate_limit", "ratelimit", "too_many_requests")
    ):
        return _ProviderFailure(
            "rate_limited",
            summary,
            retry_after_seconds=_retry_after_seconds(headers),
        )

    quota_like = any(
        token in lowered
        for token in (
            "quota",
            "insufficient funds",
            "add funds",
            "account balance",
            "credit exhausted",
            "credits exhausted",
        )
    )
    if quota_like:
        return _ProviderFailure("quota_review_required", summary)

    if status in {401, 403} or any(
        token in codes
        for token in ("unauthorized", "authentication", "invalid_api_key", "access_denied")
    ):
        return _ProviderFailure("authentication_failed", summary)

    if status == 408 or status >= 500:
        return _ProviderFailure(
            "service_unavailable",
            summary,
            retry_after_seconds=_retry_after_seconds(headers),
        )

    if 400 <= status < 500:
        return _ProviderFailure("invalid_request", summary)

    return _ProviderFailure("provider_error", summary)


def _retry_after_seconds(headers: Mapping[str, Any]) -> float | None:
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


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
    failure = _classify_provider_error(status, body, {})
    return failure is not None and failure.kind == "free_tier_exhausted"


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
