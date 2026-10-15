from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from watt_profile.contracts.upload import validate_upload_body
from watt_profile.logger.settings import LoggerSettings
from watt_profile.logger.storage.repository import PendingGroup
from watt_profile.logger.upload import UploadTransport


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _settings(**changes) -> LoggerSettings:
    values = dict(
        data_dir=Path("/tmp/logger"),
        host="127.0.0.1",
        port=8080,
        input_mode="synthetic",
        operator_user="operator",
        operator_password_file=Path("/tmp/password"),
        cookie_secure=False,
        dev_server_origin="http://127.0.0.1:18081",
        dev_server_connect_origin="http://server:8080",
    )
    values.update(changes)
    return LoggerSettings(**values)


def _group() -> PendingGroup:
    data = json.loads((Path(__file__).parents[1] / "examples/ingest/request.json").read_bytes())
    batch = validate_upload_body(json.dumps(data, separators=(",", ":")).encode())
    return PendingGroup(
        batch=batch,
        canonical_records=(),
        body=json.dumps(data, separators=(",", ":")).encode(),
    )


def _confirmation(group: PendingGroup) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "device_id": str(group.batch.device_id),
            "confirmed_record_ids": [str(record.record_id) for record in group.batch.records],
        },
        separators=(",", ":"),
    ).encode()


class _BytesStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        yield self.body


@pytest.mark.anyio
async def test_exact_stored_body_headers_and_internal_host_port() -> None:
    group = _group()
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = await request.aread()
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_BytesStream(_confirmation(group)),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = UploadTransport(_settings(), client=client)
    result = await transport.send("http://127.0.0.1:18081", "secret", group)
    assert result.confirmation is not None
    assert seen["url"] == "http://server:8080/api/v1/ingest/batches"
    assert seen["headers"]["host"] == "127.0.0.1:18081"
    assert seen["headers"]["authorization"] == "Bearer secret"
    assert seen["headers"]["accept-encoding"] == "identity"
    assert seen["body"] == group.body
    await transport.aclose()


@pytest.mark.anyio
async def test_nonmatching_origin_routes_directly_and_preserves_host_port() -> None:
    group = _group()
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host"] = request.headers["host"]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_BytesStream(_confirmation(group)),
        )

    transport = UploadTransport(
        _settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await transport.send("https://example.test:9443", "secret", group)
    assert seen == {
        "url": "https://example.test:9443/api/v1/ingest/batches",
        "host": "example.test:9443",
    }
    await transport.aclose()


@pytest.mark.anyio
async def test_unicode_origins_use_ascii_authority_and_unroutable_origin_is_safe() -> None:
    group = _group()
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host"] = request.headers["host"]
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_BytesStream(_confirmation(group)),
        )

    settings = _settings(
        dev_server_origin="http://bücher.example:18081",
        dev_server_connect_origin="http://server:8080",
    )
    transport = UploadTransport(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    result = await transport.send("http://bücher.example:18081", "secret", group)
    assert result.confirmation is not None
    assert seen == {
        "url": "http://server:8080/api/v1/ingest/batches",
        "host": "xn--bcher-kva.example:18081",
    }
    result = await transport.send("http://☃.example", "secret", group)
    assert result.error_code == "upload_endpoint_error"
    await transport.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-type": "text/html"}, b"{}"),
        ({"content-type": "application/json", "content-encoding": "gzip"}, b"{}"),
        ({"content-type": "application/json"}, b'{"schema_version":1,"schema_version":1}'),
    ],
)
async def test_invalid_confirmation_never_confirms(headers, body) -> None:
    group = _group()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers=headers, stream=_BytesStream(body))
        )
    )
    transport = UploadTransport(_settings(), client=client)
    result = await transport.send("https://example.test", "secret", group)
    assert result.error_code == "invalid_confirmation"
    await transport.aclose()


@pytest.mark.anyio
async def test_response_limit_redirect_and_connection_failure_are_safe() -> None:
    group = _group()
    for status, headers, body, expected in (
        (200, {"content-type": "application/json"}, b" " * 65_537, "invalid_confirmation"),
        (307, {"location": "https://elsewhere.invalid"}, b"", "upload_endpoint_error"),
        (201, {"content-type": "application/json"}, b"{}", "invalid_confirmation"),
    ):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, status=status, headers=headers, body=body: httpx.Response(
                    status, headers=headers, stream=_BytesStream(body)
                )
            )
        )
        transport = UploadTransport(_settings(), client=client)
        result = await transport.send("https://example.test", "secret", group)
        assert result.error_code == expected
        await transport.aclose()

    async def failed(request):
        raise httpx.ConnectError("private detail", request=request)

    transport = UploadTransport(
        _settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(failed))
    )
    result = await transport.send("https://example.test", "secret", group)
    assert result.error_code == "upload_network_error"
    await transport.aclose()


@pytest.mark.anyio
async def test_error_envelope_requires_strict_shape_and_matching_http_status() -> None:
    group = _group()
    for status, payload, expected in (
        (
            401,
            {
                "schema_version": 1,
                "error": {
                    "code": "invalid_credential",
                    "message": "Upload credential is invalid or disabled.",
                    "retryable": False,
                },
            },
            "invalid_credential",
        ),
        (
            403,
            {
                "schema_version": 1,
                "error": {
                    "code": "invalid_credential",
                    "message": "Upload credential is invalid or disabled.",
                    "retryable": False,
                },
            },
            "upload_http_rejected",
        ),
        (
            401,
            {
                "error": {
                    "code": "invalid_credential",
                    "message": "Upload credential is invalid or disabled.",
                    "retryable": False,
                },
            },
            "upload_http_rejected",
        ),
    ):
        body = json.dumps(payload, separators=(",", ":")).encode()
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, status=status, body=body: httpx.Response(
                    status, headers={"content-type": "application/json"}, stream=_BytesStream(body)
                )
            )
        )
        transport = UploadTransport(_settings(), client=client)
        result = await transport.send("https://example.test", "secret", group)
        assert result.error_code == expected
        await transport.aclose()


@pytest.mark.anyio
async def test_deadline_and_cancellation_are_injectable_and_close_stream() -> None:
    group = _group()
    closed = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        async def stream():
            try:
                await asyncio.sleep(60)
                yield b"{}"
            finally:
                closed.set()

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                async for chunk in stream():
                    yield chunk

        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=SlowStream()
        )

    async def deadline(awaitable, seconds):
        assert seconds == pytest.approx(30)
        return await asyncio.wait_for(awaitable, 0.01)

    transport = UploadTransport(
        _settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        deadline_runner=deadline,
    )
    result = await transport.send("https://example.test", "secret", group)
    assert result.error_code == "upload_timeout"
    assert closed.is_set()
    await transport.aclose()
