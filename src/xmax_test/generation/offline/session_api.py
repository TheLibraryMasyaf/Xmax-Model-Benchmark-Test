"""Session API client for the Session + RTC offline backend.

- ``POST /session``
- ``PUT /session/{sessionUid}/heartbeat``
- ``DELETE /session/{sessionUid}``

The real client is HTTP-based; the fake is scriptable for offline tests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from ...errors import ExternalServiceError


class SessionApiClient(Protocol):
    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def heartbeat(self, session_uid: str) -> dict[str, Any]: ...

    def close_session(self, session_uid: str) -> dict[str, Any]: ...


class HttpSessionApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: int = 60,
        max_retries: int = 2,
        log_events: list[dict[str, Any]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._log = log_events if log_events is not None else []

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/session", payload)

    def heartbeat(self, session_uid: str) -> dict[str, Any]:
        return self._request("PUT", f"/session/{session_uid}/heartbeat", {})

    def close_session(self, session_uid: str) -> dict[str, Any]:
        return self._request("DELETE", f"/session/{session_uid}", {})

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            try:
                data = json.dumps(payload).encode("utf-8")
                request = urllib.request.Request(
                    f"{self._base_url}{path}",
                    data=data,
                    method=method,
                    headers={
                        "Content-Type": "application/json",
                        **({"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}),
                    },
                )
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    raw = response.read()
                self._log.append(
                    {"method": method, "path": path, "attempt": attempt, "status": "ok"}
                )
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                self._log.append(
                    {
                        "method": method,
                        "path": path,
                        "attempt": attempt,
                        "status": exc.code,
                    }
                )
                if exc.code in {401, 403}:
                    raise ExternalServiceError(
                        f"session api auth failed ({exc.code}): {path}"
                    ) from exc
                if exc.code in {402, 429}:
                    raise ExternalServiceError(
                        f"session api quota/concurrency (HTTP {exc.code}): {path}"
                    ) from exc
                if attempt > self._max_retries:
                    raise ExternalServiceError(
                        f"session api HTTP {exc.code} after {attempt} attempts: {path}"
                    ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._log.append(
                    {
                        "method": method,
                        "path": path,
                        "attempt": attempt,
                        "status": "network",
                    }
                )
                if attempt > self._max_retries:
                    raise ExternalServiceError(
                        f"session api network failure after {attempt} attempts: {exc}"
                    ) from exc


class FakeSessionApiClient:
    """Deterministic scriptable session API fake."""

    def __init__(
        self,
        *,
        session: dict[str, Any] | None = None,
        fail_create: str | None = None,
        fail_heartbeat: str | None = None,
        fail_close: str | None = None,
    ) -> None:
        self._session = session or {
            "sessionUid": "session-fake-1",
            "rtc": {"room": "room-1", "token": "rtc-token"},
        }
        self._fail_create = fail_create
        self._fail_heartbeat = fail_heartbeat
        self._fail_close = fail_close
        self.calls: list[str] = []
        self.created = False

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create_session")
        if self._fail_create:
            raise ExternalServiceError(self._fail_create)
        self.created = True
        return self._session

    def heartbeat(self, session_uid: str) -> dict[str, Any]:
        self.calls.append("heartbeat")
        if self._fail_heartbeat:
            raise ExternalServiceError(self._fail_heartbeat)
        return {"ok": True}

    def close_session(self, session_uid: str) -> dict[str, Any]:
        self.calls.append("close_session")
        if self._fail_close:
            raise ExternalServiceError(self._fail_close)
        return {"ok": True}
