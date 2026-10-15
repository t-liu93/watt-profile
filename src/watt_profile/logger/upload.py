"""Bounded asynchronous HTTP transport for stored Logger upload batches."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from watt_profile.logger.storage.repository import PendingGroup, PendingGroupKey

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
class UploadObservation:
    state: str = "idle"
    global_error: str | None = None
    next_attempt_at: datetime | None = None
    blocked_group_count: int = 0


async def _wait_for_signal(delay: float, wake: asyncio.Event, stop: asyncio.Event) -> str:
    """Wait for a timer, explicit Retry, or shutdown without polling."""
    timer = asyncio.create_task(asyncio.sleep(max(0.0, delay)))
    awakened = asyncio.create_task(wake.wait())
    stopped = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            (timer, awakened, stopped), return_when=asyncio.FIRST_COMPLETED
        )
        if stopped in done and stop.is_set():
            return "stop"
        if awakened in done and wake.is_set():
            wake.clear()
            return "wake"
        return "timer"
    finally:
        for task in (timer, awakened, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(timer, awakened, stopped, return_exceptions=True)


class UploadScheduler:
    """One-flight scheduler for durable Logger batches.

    The transport is injectable and must expose ``send(origin, token, group)``.
    Repository work is always moved to a thread and shielded until the short
    transaction has completed, including when this coroutine is cancelled.
    """

    RETRY_DELAYS = (1, 2, 4, 8, 16, 32, 60)

    def __init__(
        self,
        repository,
        transport,
        token: str | Callable[[], str],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float, asyncio.Event, asyncio.Event], Awaitable[str]] = _wait_for_signal,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_fatal: Callable[[str], Any] | None = None,
    ) -> None:
        self.repository = repository
        self.transport = transport
        self._token = token
        self._monotonic = monotonic
        self._wait = wait
        self._utcnow = utcnow
        self._on_fatal = on_fatal
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._origin: tuple[str, str, int] | None = None
        self._global_error: str | None = None
        self._global_blocked = False
        self._blocked: dict[PendingGroupKey, str] = {}
        self._limits: dict[PendingGroupKey, int] = {}
        self._failures = 0
        self._retry_at: float | None = None
        self._retry_group: PendingGroup | None = None
        self._state = "idle"
        self._fatal = False
        self._running = False
        self._retry_requested = False

    @property
    def observation(self) -> UploadObservation:
        when = None
        if self._retry_at is not None:
            when = self._utcnow() + timedelta(seconds=max(0, self._retry_at - self._monotonic()))
        return UploadObservation(
            state=self._state,
            global_error=self._global_error,
            next_attempt_at=when,
            blocked_group_count=len(self._blocked),
        )

    async def _db(self, function, *args, **kwargs):
        operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # asyncio cancellation cannot stop a worker thread or its SQLite transaction.
            try:
                await asyncio.shield(operation)
            finally:
                raise

    def retry(self) -> None:
        # Apply reset only at a worker boundary so an in-flight response cannot
        # consume a Retry that arrived while the request was outstanding.
        self._retry_requested = True
        self._wake.set()

    def wake(self) -> None:
        """Recheck settings or pending work without clearing origin-scoped blocks."""
        self._wake.set()

    @property
    def blocked_group_errors(self) -> dict[PendingGroupKey, str]:
        return dict(self._blocked)

    def _apply_retry(self) -> None:
        if not self._retry_requested:
            return
        self._retry_requested = False
        self._global_error = None
        self._global_blocked = False
        self._blocked.clear()
        self._limits.clear()
        self._failures = 0
        self._retry_at = None
        self._retry_group = None

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @staticmethod
    def _key(group: PendingGroup) -> PendingGroupKey:
        batch = group.batch
        return (
            batch.device_id,
            batch.site_id,
            batch.session.session_id,
            batch.source.source_id,
            batch.measurement_point_id,
        )

    async def _fatal_error(self, code: str) -> None:
        self._fatal = True
        self._state = "failed"
        self._global_error = code
        if self._on_fatal is not None:
            result = self._on_fatal(code)
            if asyncio.iscoroutine(result):
                await result

    async def _refresh_origin(self):
        try:
            installation = await self._db(self.repository.load_installation)
        except Exception:
            await self._fatal_error("storage_error")
            return None
        origin = installation.settings.server_url if installation is not None else None
        identity = origin_identity(origin) if origin is not None and valid_origin(origin) else None
        if identity != self._origin:
            self._origin = identity
            self._global_error = None
            self._global_blocked = False
            self._blocked.clear()
            self._limits.clear()
            self._retry_group = None
            self._failures = 0
            self._retry_at = None
        return origin if identity is not None else None

    def _delay_from(self, retry_after: str | None) -> float:
        if retry_after is not None:
            value = retry_after.strip()
            if re.fullmatch(r"[0-9]+", value):
                # Cap before integer conversion so arbitrarily large but valid
                # delay-seconds remain a valid, bounded Retry-After value.
                significant = value.lstrip("0")
                delay = 0 if not significant else 60 if len(significant) > 2 else int(significant)
                return float(min(60, max(1, delay)))
            try:
                from email.utils import parsedate_to_datetime

                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                delay = (parsed - self._utcnow()).total_seconds()
                return min(60.0, max(1.0, delay))
            except (TypeError, ValueError, OverflowError):
                pass
        delay = self.RETRY_DELAYS[min(self._failures, len(self.RETRY_DELAYS) - 1)]
        self._failures += 1
        return float(delay)

    async def _pause(self, delay: float) -> bool:
        self._retry_at = self._monotonic() + delay
        self._state = "backoff"
        signal = await self._wait(delay, self._wake, self._stop)
        if signal == "timer":
            self._retry_at = None
        return signal == "stop"

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("upload_worker_already_running")
        self._running = True
        self._state = "idle"
        try:
            while not self._stop.is_set() and not self._fatal:
                self._apply_retry()
                origin = await self._refresh_origin()
                if self._fatal:
                    break
                if self._retry_requested:
                    self._apply_retry()
                    continue
                if origin is None:
                    self._state = "idle"
                    if await self._wait(10.0, self._wake, self._stop) == "stop":
                        break
                    continue
                if self._global_blocked:
                    self._state = "blocked"
                    if await self._wait(10.0, self._wake, self._stop) == "stop":
                        break
                    continue
                if self._retry_at is not None:
                    if await self._pause(max(0, self._retry_at - self._monotonic())):
                        break
                    continue
                try:
                    group = self._retry_group
                    if group is None:
                        group = await self._db(
                            self.repository.select_pending_group,
                            excluded_groups=tuple(self._blocked),
                        )
                        if group is not None and self._key(group) in self._limits:
                            group = await self._db(
                                self.repository.select_pending_group,
                                limit=self._limits[self._key(group)],
                                excluded_groups=tuple(self._blocked),
                            )
                except ValueError as exc:
                    if str(exc) == "batch_too_large":
                        # The offending one-record group is selected with an unbounded body
                        # only to identify and block it; normal contract size is still enforced.
                        await self._fatal_error("storage_error")
                        break
                    await self._fatal_error("storage_error")
                    break
                except Exception:
                    await self._fatal_error("storage_error")
                    break
                if self._retry_requested:
                    self._apply_retry()
                    continue
                if group is None:
                    self._state = "blocked" if self._blocked else "idle"
                    if await self._wait(10.0, self._wake, self._stop) == "stop":
                        break
                    continue
                self._retry_group = None
                key = self._key(group)
                if self._stop.is_set():
                    break
                try:
                    self._state = "sending"
                    token = self._token() if callable(self._token) else self._token
                    sending = asyncio.create_task(self.transport.send(origin, token, group))
                    stopping = asyncio.create_task(self._stop.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (sending, stopping), return_when=asyncio.FIRST_COMPLETED
                        )
                        if stopping in done and self._stop.is_set() and not sending.done():
                            sending.cancel()
                            await asyncio.gather(sending, return_exceptions=True)
                            break
                        result = await sending
                    finally:
                        if not sending.done():
                            sending.cancel()
                        if not stopping.done():
                            stopping.cancel()
                        await asyncio.gather(sending, stopping, return_exceptions=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._retry_group = group
                    self._global_error = "upload_network_error"
                    self._global_blocked = False
                    delay = self._delay_from(None)
                    if await self._pause(delay):
                        break
                    continue
                status, code = result.status_code, result.error_code
                if status == 200 and result.confirmation is not None and code is None:
                    try:
                        await self._db(
                            self.repository.apply_confirmation,
                            group.batch,
                            result.confirmation,
                            self._utcnow(),
                        )
                    except InvalidConfirmation:
                        code = "invalid_confirmation"
                    except Exception as exc:
                        if _is_corruption(exc):
                            await self._fatal_error("storage_error")
                            break
                        if isinstance(exc, ValueError):
                            await self._fatal_error("storage_error")
                            break
                        # Keep the exact group and its retry schedule on local write failure.
                        self._retry_group = group
                        self._global_error = "upload_storage_error"
                        if await self._pause(self._delay_from(None)):
                            break
                        continue
                    else:
                        self._failures = 0
                        self._retry_group = None
                        self._global_error = None
                        await asyncio.sleep(0)
                        continue
                if status == 401 or code == "invalid_credential":
                    self._global_error = "invalid_credential"
                    self._global_blocked = True
                    self._retry_group = None
                    self._state = "blocked"
                    continue
                if 300 <= status < 400 or status in {404, 405} or code == "upload_endpoint_error":
                    self._global_error = "upload_endpoint_error"
                    self._global_blocked = True
                    self._retry_group = None
                    self._state = "blocked"
                    continue
                if status == 413:
                    current = self._limits.get(key, 200)
                    if len(group.batch.records) <= 1:
                        self._blocked[key] = "batch_too_large"
                    else:
                        self._limits[key] = max(1, current // 2)
                    self._retry_group = None
                    continue
                if status in {403, 409, 422} or 400 <= status < 500 and status != 429:
                    self._blocked[key] = code or "upload_http_rejected"
                    self._retry_group = None
                    continue
                if status == 200 or 200 <= status < 300:
                    code = code or "invalid_confirmation"
                elif status == 429 or 500 <= status < 600 or status == 0:
                    code = code or "upload_network_error"
                else:
                    code = "upload_protocol_error"
                self._global_error = code or "upload_network_error"
                self._global_blocked = False
                self._retry_group = group
                if await self._pause(self._delay_from(result.retry_after)):
                    break
                # The error observation remains available during backoff and is cleared
                # only after a durable confirmation or explicit Retry/origin change.
            self._state = "stopping" if not self._fatal else "failed"
        finally:
            self._running = False
            if not self._fatal:
                self._state = "stopping"


def _is_corruption(error: Exception) -> bool:
    from watt_profile.logger.maintenance import is_database_corruption

    return is_database_corruption(error)


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
