"""HTTP transport: retries, rate limiting, and a dry-run recorder.

`DryRunSession` implements the same surface as `Session`, so every target walks
its entire create -> upload -> publish path under --dry-run without a socket
being opened. That is the mechanism that lets this package be exercised
end-to-end before anyone points it at a real repository.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

DEFAULT_TIMEOUT: Tuple[float, float] = (10.0, 300.0)
#: Preflight reads reference lists that are advisory - if one is unreachable the
#: publish should carry on, not spend a retry budget discovering that.
REFERENCE_READ_RETRIES = 0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
SENSITIVE_HEADERS = {"authorization", "x-dataverse-key"}
SENSITIVE_PARAMS = {"access_token", "token"}


class TransportError(Exception):
    """A request failed in a way retrying will not fix."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def redact_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {
        key: ("<redacted>" if key.lower() in SENSITIVE_HEADERS else value)
        for key, value in (headers or {}).items()
    }


def redact_url(url: str) -> str:
    for param in SENSITIVE_PARAMS:
        marker = f"{param}="
        if marker in url:
            head, _, tail = url.partition(marker)
            _, sep, rest = tail.partition("&")
            url = f"{head}{marker}<redacted>{sep}{rest}"
    return url


@dataclass
class RecordedCall:
    method: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    json_body: Optional[Any] = None
    data_summary: Optional[str] = None
    files_summary: Optional[str] = None
    params: Optional[Dict[str, Any]] = None

    def describe(self) -> str:
        lines = [f"{self.method} {self.url}"]
        if self.params:
            rendered = "&".join(f"{k}={v}" for k, v in self.params.items())
            lines.append(f"    params: {rendered}")
        for key, value in self.headers.items():
            lines.append(f"    {key}: {value}")
        if self.json_body is not None:
            body = json.dumps(self.json_body, indent=2, default=str)
            lines.append("    body:")
            lines.extend(f"      {line}" for line in body.splitlines())
        if self.data_summary:
            lines.append(f"    data: {self.data_summary}")
        if self.files_summary:
            lines.append(f"    files: {self.files_summary}")
        return "\n".join(lines)


class Session:
    """Thin wrapper over requests with retries, backoff, and optional throttling."""

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
        max_retries: int = 4,
        min_interval: float = 0.0,
        logger: Optional[Callable[[str], None]] = None,
    ):
        self.session = requests.Session()
        self.base_headers = dict(headers or {})
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self.logger = logger
        self._last_request_at = 0.0
        self.calls: List[RecordedCall] = []

    # -- public API -------------------------------------------------------

    @property
    def dry_run(self) -> bool:
        return False

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Any] = None,
        data: Any = None,
        files: Any = None,
        params: Optional[Dict[str, Any]] = None,
        expected: Tuple[int, ...] = (200, 201, 202, 204),
        retries: Optional[int] = None,
        omit_base_headers: bool = False,
    ) -> requests.Response:
        # Presigned upload URLs point at a third party (S3), so the repository
        # API token must not ride along on those requests.
        merged = dict(headers or {}) if omit_base_headers else {**self.base_headers, **(headers or {})}
        self._record(method, url, merged, json_body, data, files, params)
        self._log(f"{method} {redact_url(url)}")

        budget = self.max_retries if retries is None else retries
        last_error: Optional[TransportError] = None
        for attempt in range(budget + 1):
            self._throttle()
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=merged,
                    json=json_body,
                    data=data,
                    files=files,
                    params=params,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = TransportError(f"{method} {redact_url(url)} failed: {exc}")
                if attempt < budget:
                    self._backoff(attempt, None)
                    continue
                raise last_error from exc

            if response.status_code in expected:
                return response

            if response.status_code in RETRY_STATUSES and attempt < budget:
                self._log(f"  -> {response.status_code}, retrying")
                self._backoff(attempt, response)
                _rewind(data, files)
                continue

            raise TransportError(
                f"{method} {redact_url(url)} returned {response.status_code}",
                status_code=response.status_code,
                body=response.text[:2000],
            )

        raise last_error or TransportError(f"{method} {redact_url(url)} exhausted retries")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", url, **kwargs)

    # -- internals --------------------------------------------------------

    def _record(self, method, url, headers, json_body, data, files, params=None) -> None:
        self.calls.append(
            RecordedCall(
                method=method,
                url=redact_url(url),
                headers=redact_headers(headers),
                json_body=json_body,
                data_summary=_summarize(data),
                files_summary=_summarize(files),
                params=dict(params) if params else None,
            )
        )

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger(message)

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int, response: Optional[requests.Response]) -> None:
        delay = 2.0**attempt
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
        time.sleep(min(delay, 60.0))


class DryRunSession(Session):
    """Records calls and returns canned responses. Never opens a connection."""

    def __init__(self, headers: Optional[Dict[str, str]] = None, logger: Optional[Callable[[str], None]] = None):
        super().__init__(headers=headers, logger=logger)
        self.responders: List[Tuple[Callable[[str, str], bool], Dict[str, Any]]] = []

    @property
    def dry_run(self) -> bool:
        return True

    def add_responder(self, predicate: Callable[[str, str], bool], payload: Dict[str, Any]) -> None:
        """Register a canned JSON body for calls matching (method, url)."""
        self.responders.append((predicate, payload))

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json_body: Optional[Any] = None,
        data: Any = None,
        files: Any = None,
        params: Optional[Dict[str, Any]] = None,
        expected: Tuple[int, ...] = (200, 201, 202, 204),
        retries: Optional[int] = None,
        omit_base_headers: bool = False,
    ) -> "FakeResponse":
        merged = dict(headers or {}) if omit_base_headers else {**self.base_headers, **(headers or {})}
        self._record(method, url, merged, json_body, data, files, params)
        self._log(f"[dry-run] {method} {redact_url(url)}")

        for predicate, payload in self.responders:
            if predicate(method, url):
                return FakeResponse(expected[0] if expected else 200, payload)
        return FakeResponse(expected[0] if expected else 200, {})


class FakeResponse:
    """Minimal stand-in for requests.Response under dry run."""

    def __init__(self, status_code: int, payload: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers: Dict[str, str] = {}

    def json(self) -> Dict[str, Any]:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")


def _summarize(value: Any) -> Optional[str]:
    """Describe a body without dumping file contents into a log."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if isinstance(item, tuple):
                filename, content = item[0], item[1] if len(item) > 1 else None
                if filename is None:
                    # (None, value, content_type) is an ordinary multipart form field.
                    parts.append(f"{key}={str(content)[:400]}")
                else:
                    parts.append(f"{key}=<file {filename}>")
            elif isinstance(item, (bytes, bytearray)):
                parts.append(f"{key}=<{len(item)} bytes>")
            elif hasattr(item, "read"):
                parts.append(f"{key}=<stream {getattr(item, 'name', '?')}>")
            else:
                text = str(item)
                parts.append(f"{key}={text[:200]}")
        return ", ".join(parts)
    if hasattr(value, "read"):
        return f"<stream {getattr(value, 'name', '?')}>"
    return str(value)[:200]


def _rewind(data: Any, files: Any) -> None:
    """Reset file handles before a retry, or the retry uploads nothing."""
    for candidate in (data, files):
        if hasattr(candidate, "seek"):
            candidate.seek(0)
        elif isinstance(candidate, dict):
            for item in candidate.values():
                if hasattr(item, "seek"):
                    item.seek(0)
                elif isinstance(item, tuple) and len(item) > 1 and hasattr(item[1], "seek"):
                    item[1].seek(0)
