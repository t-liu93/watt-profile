"""Bounded asynchronous HTTP transport for stored Logger upload batches."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from watt_profile.contracts.upload import (
    ERROR_CONTRACTS,
    InvalidConfirmation,
    UploadErrorResponse,
    validate_confirmation_body,
)
from watt_profile.logger.settings import LoggerSettings
from watt_profile.logger.storage.database import origin_identity, valid_origin
from watt_profile.logger.storage.repository import PendingGroup

MAX_RESPONSE_BYTES = 65_536
UPLOAD_PATH = "/api/v1/ingest/batches"


@dataclass(frozen=True, slots=True)
class TransportResult:
    status_code: int
    confirmation: Any | None = None
    error_code: str | None = None
    retry_after: str | None = None


class UploadTransport:
    """Owns one pooled client; callers own scheduling and local acknowledgement."""

    def __init__(
        self,
        settings: LoggerSettings,
        *,
        client: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        deadline_runner: Callable[[Awaitable[Any], float], Awaitable[Any]] | None = None,
    ) -> None:
        self.settings = settings
        self._monotonic = monotonic
        self._deadline_runner = deadline_runner or self._run_deadline
        self._client = client or httpx.AsyncClient(
            verify=True,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=5.0, pool=5.0, read=30.0, write=30.0),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    @staticmethod
    async def _run_deadline(awaitable: Awaitable[Any], seconds: float) -> Any:
        async with asyncio.timeout(seconds):
            return await awaitable

    async def aclose(self) -> None:
        await self._client.aclose()

    def _destination(self, saved_origin: str) -> tuple[str, str]:
        if not valid_origin(saved_origin):
            raise ValueError("invalid_upload_origin")
        expected = self.settings.dev_server_origin
        connect = self.settings.dev_server_connect_origin
        if (
            expected is not None
            and connect is not None
            and origin_identity(saved_origin) == origin_identity(expected)
        ):
            internal = urlsplit(connect)
            target = f"{internal.scheme}://{internal.netloc}{UPLOAD_PATH}"
            return target, httpx.URL(expected).netloc.decode("ascii")
        target = f"{saved_origin}{UPLOAD_PATH}"
        return target, httpx.URL(target).netloc.decode("ascii")

    async def send(
        self,
        saved_origin: str,
        token: str,
        group: PendingGroup,
    ) -> TransportResult:
        try:
            url, host = self._destination(saved_origin)
        except (ValueError, UnicodeError, httpx.InvalidURL):
            return TransportResult(0, error_code="upload_endpoint_error")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Host": host,
        }
        started = self._monotonic()
        try:
            response = await self._deadline_runner(
                self._exchange(url, headers, group.body),
                max(0.0, 30.0 - (self._monotonic() - started)),
            )
        except (TimeoutError, httpx.TimeoutException):
            return TransportResult(0, error_code="upload_timeout")
        except (httpx.RequestError, httpx.InvalidURL, UnicodeError, ValueError):
            return TransportResult(0, error_code="upload_network_error")
        if response.status_code == 200:
            if (
                response.encoded
                or response.oversized
                or not _is_json(response.headers.get("content-type"))
            ):
                return TransportResult(200, error_code="invalid_confirmation")
            try:
                confirmation = validate_confirmation_body(response.body, group.batch)
            except InvalidConfirmation:
                return TransportResult(200, error_code="invalid_confirmation")
            return TransportResult(200, confirmation=confirmation)
        if 300 <= response.status_code < 400 or response.status_code in {404, 405}:
            return TransportResult(
                response.status_code,
                error_code="upload_endpoint_error",
                retry_after=response.headers.get("retry-after"),
            )
        if 200 <= response.status_code < 300:
            return TransportResult(
                response.status_code,
                error_code="invalid_confirmation",
                retry_after=response.headers.get("retry-after"),
            )
        code = _parse_error(
            response.body, response.headers.get("content-type"), response.status_code
        )
        return TransportResult(
            response.status_code, error_code=code, retry_after=response.headers.get("retry-after")
        )

    async def _exchange(self, url: str, headers: dict[str, str], body: bytes) -> _Response:
        async with self._client.stream("POST", url, headers=headers, content=body) as response:
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if encoding not in {"", "identity"}:
                return _Response(response.status_code, dict(response.headers), b"", encoded=True)
            if response.is_stream_consumed:
                data = response.content
                return _Response(
                    response.status_code,
                    dict(response.headers),
                    data,
                    oversized=len(data) > MAX_RESPONSE_BYTES,
                )
            chunks: list[bytes] = []
            length = 0
            async for chunk in response.aiter_raw():
                length += len(chunk)
                if length > MAX_RESPONSE_BYTES:
                    return _Response(
                        response.status_code, dict(response.headers), b"", oversized=True
                    )
                chunks.append(chunk)
            return _Response(response.status_code, dict(response.headers), b"".join(chunks))


@dataclass(frozen=True, slots=True)
class _Response:
    status_code: int
    headers: dict[str, str]
    body: bytes
    oversized: bool = False
    encoded: bool = False


def _is_json(value: str | None) -> bool:
    if value is None:
        return False
    media = value.split(";", 1)[0].strip().lower()
    return media == "application/json" or media.endswith("+json")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _parse_error(body: bytes, content_type: str | None, status: int) -> str:
    if not _is_json(content_type):
        return "upload_http_rejected"
    try:
        parsed = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs)
        if type(parsed) is not dict or set(parsed) != {"schema_version", "error"}:
            return "upload_http_rejected"
        if type(parsed.get("schema_version")) is not int or parsed["schema_version"] != 1:
            return "upload_http_rejected"
        error = UploadErrorResponse.model_validate(parsed).error
        expected = ERROR_CONTRACTS.get(error.code)
        status_matches = expected is not None and expected[0] == status
        if error.code == "temporarily_unavailable":
            status_matches = status == 429 or status >= 500
        if status_matches:
            return error.code
    except (ValueError, TypeError, UnicodeError, ValidationError, RecursionError):
        pass
    return "upload_http_rejected"
