"""Independent Server HTTP boundary with bounded input and process-local authentication."""

import json
from contextlib import asynccontextmanager
from functools import partial
from html import escape
from ipaddress import ip_address
from urllib.parse import parse_qs, urlsplit

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from watt_profile.contracts.upload import MAX_BODY_BYTES, UploadValidationError
from watt_profile.server.auth import COOKIE, LOGIN_COOKIE, MAX_AGE, AdministratorAuth
from watt_profile.server.bootstrap import initialize_storage
from watt_profile.server.export import ExportManager
from watt_profile.server.ingest import IngestService
from watt_profile.server.queries import (
    InvalidQuery,
    QueryService,
    QueryUnavailable,
    ReadingPageOptions,
    parse_filters,
    query_id,
)
from watt_profile.server.registration import (
    CredentialInput,
    DeviceInput,
    InvalidRegistration,
    ObjectNotFound,
    PageOptions,
    RegistrationConflict,
    RegistrationSummary,
    SiteInput,
    SitePatch,
    SiteSearch,
)
from watt_profile.server.settings import ServerSettings, _valid_public_origin
from watt_profile.server.storage.ingest import IngestRepository
from watt_profile.server.storage.registration import RegistrationRepository, StorageUnavailable

PREFIX = "/api/admin/v1"
MESSAGES = {
    "authentication_required": (401, "Administrator authentication is required."),
    "invalid_credentials": (401, "Administrator credentials are invalid."),
    "csrf_rejected": (403, "Request verification failed."),
    "invalid_host": (403, "Request host is invalid."),
    "object_not_found": (404, "Object was not found."),
    "registration_conflict": (409, "Registration conflicts with existing credentials."),
    "invalid_input": (422, "Input does not satisfy the management contract."),
    "login_throttled": (429, "Too many login attempts."),
    "temporarily_unavailable": (503, "Service is temporarily unavailable."),
}


class ManagementError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def failure(code):
    status, message = MESSAGES[code]
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def origin_key(value):
    if not _valid_public_origin(value):
        return None
    parsed = urlsplit(value)
    host = parsed.hostname.lower()
    try:
        host = str(ip_address(host))
    except ValueError:
        pass
    return (
        parsed.scheme,
        host,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


class Boundary:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        settings = self.settings()
        if settings is not None:
            headers = {key.lower(): value for key, value in scope["headers"]}
            host = headers.get(b"host", b"").decode("latin-1")
            expected = origin_key(settings.public_origin)
            if origin_key(expected[0] + "://" + host) != expected:
                return await failure("invalid_host")(scope, receive, send)

        async def safe_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *((key, value) for key, value in message["headers"] if key != b"cache-control"),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        await self.app(scope, receive, safe_send)


async def bounded_body(request, maximum, *, ingest=False):
    def reject():
        if ingest:
            raise UploadValidationError("batch_too_large")
        raise ManagementError("invalid_input")

    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            length = int(raw_length)
            if length < 0:
                raise ValueError
        except ValueError:
            if ingest:
                raise UploadValidationError("invalid_payload") from None
            raise ManagementError("invalid_input") from None
        if length > maximum:
            reject()
    result = bytearray()
    async for chunk in request.stream():
        if len(result) + len(chunk) > maximum:
            reject()
        result.extend(chunk)
    return bytes(result)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManagementError("invalid_input")
        result[key] = value
    return result


async def json_input(request, model):
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ManagementError("invalid_input")
    if request.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise ManagementError("invalid_input")
    body = await bounded_body(request, 32 * 1024)
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError
        if model is None:
            if value:
                raise ValueError
            return value
        return model.model_validate(value)
    except (ValueError, TypeError, RecursionError):
        raise ManagementError("invalid_input") from None


def query_values(request, allowed):
    pairs = list(request.query_params.multi_items())
    if any(key not in allowed for key, _ in pairs) or len({key for key, _ in pairs}) != len(pairs):
        raise ManagementError("invalid_input")
    values = dict(pairs)
    for key in ("offset", "limit"):
        if key in values:
            try:
                values[key] = int(values[key])
            except ValueError:
                raise ManagementError("invalid_input") from None
    return values


class OwnedStreamingResponse(StreamingResponse):
    def __init__(self, stream):
        self.stream = stream

        async def chunks():
            while True:
                # Allow disconnect to leave a blocked worker; close() below then
                # cancels the driver's FETCH and waits for that worker's lock.
                chunk = await anyio.to_thread.run_sync(next, stream, None, abandon_on_cancel=True)
                if chunk is None:
                    return
                yield chunk

        super().__init__(
            chunks(),
            media_type=stream.media_type,
            headers={"Content-Disposition": 'attachment; filename="watt-profile-readings.csv"'},
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.stream.close)


def create_app(settings: ServerSettings | None = None):
    @asynccontextmanager
    async def lifespan(app):
        try:
            configured = settings or ServerSettings.from_env()
            app.state.settings = configured
            engine = await run_in_threadpool(initialize_storage, configured)
        except Exception:
            # Uvicorn logs lifespan failures. Never expose driver/configuration
            # exceptions, which may contain a secret URL or connection options.
            raise RuntimeError("Unable to initialize Server storage.") from None
        app.state.engine = engine
        app.state.auth = AdministratorAuth(configured.admin_user, configured.admin_password)
        app.state.registration = RegistrationRepository(engine)
        app.state.queries = QueryService(engine)
        app.state.ingest = IngestService(IngestRepository(engine))
        app.state.exports = ExportManager(engine)
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            app.state.auth.clear()
            try:
                await run_in_threadpool(app.state.exports.close)
            finally:
                await run_in_threadpool(engine.dispose)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.ready = settings, False
    app.add_middleware(Boundary, settings=lambda: app.state.settings)

    @app.exception_handler(Exception)
    async def safe_error(request, exc):
        if isinstance(exc, UploadValidationError):
            return JSONResponse(exc.response.model_dump(mode="json"), status_code=exc.status_code)
        code = "temporarily_unavailable"
        if isinstance(exc, ManagementError):
            code = exc.code
        elif isinstance(
            exc, (ValidationError, RequestValidationError, InvalidQuery, InvalidRegistration)
        ):
            code = "invalid_input"
        elif isinstance(exc, ObjectNotFound):
            code = "object_not_found"
        elif isinstance(exc, RegistrationConflict):
            code = "registration_conflict"
        elif isinstance(exc, HTTPException):
            code = "object_not_found" if exc.status_code == 404 else "invalid_input"
        if request.url.path == "/api/v1/ingest/batches":
            error = UploadValidationError("temporarily_unavailable")
            return JSONResponse(error.response.model_dump(mode="json"), status_code=503)
        return failure(code)

    # Specific handlers prevent framework validation details and avoid re-raising
    # expected application errors through Starlette's internal-error middleware.
    for exception in (
        ManagementError,
        UploadValidationError,
        ValidationError,
        RequestValidationError,
        InvalidQuery,
        InvalidRegistration,
        ObjectNotFound,
        RegistrationConflict,
        QueryUnavailable,
        StorageUnavailable,
        HTTPException,
    ):
        app.add_exception_handler(exception, safe_error)

    def session(request, *, mutation=False, token=None):
        if not app.state.ready:
            raise ManagementError("temporarily_unavailable")
        identifier = request.cookies.get(COOKIE)
        state = app.state.auth.session(identifier)
        if not state:
            raise ManagementError("authentication_required")
        if mutation:
            verify_origin(request)
            if not app.state.auth.valid_csrf(
                identifier, token or request.headers.get("x-csrf-token")
            ):
                raise ManagementError("csrf_rejected")
        return state

    def verify_origin(request):
        origin = request.headers.get("origin")
        if origin is not None and origin_key(origin) != origin_key(
            app.state.settings.public_origin
        ):
            raise ManagementError("csrf_rejected")

    def cookie(response, name, value, age):
        response.set_cookie(
            name,
            value,
            max_age=age,
            httponly=True,
            samesite="lax",
            secure=app.state.settings.cookie_secure,
            path="/",
        )

    @app.get("/health/live")
    async def live():
        return JSONResponse(True)

    @app.get("/health/ready")
    async def ready():
        def check():
            with app.state.engine.connect() as connection:
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                return connection.scalar(text("SELECT 1")) == 1

        healthy = False
        if app.state.ready:
            try:
                healthy = await run_in_threadpool(check)
            except SQLAlchemyError:
                pass
        return JSONResponse(healthy, status_code=200 if healthy else 503)

    @app.get("/login")
    async def login_form():
        identifier, state = app.state.auth.login_state()
        response = HTMLResponse(
            '<!doctype html><html lang="en"><title>Server login</title><h1>Server login</h1>'
            '<form method="post" action="/login">'
            f'<input type="hidden" name="csrf" value="{escape(state.csrf)}">'
            '<label>Username <input name="username" autocomplete="username"></label>'
            '<label>Password <input name="password" type="password" '
            'autocomplete="current-password"></label><button>Log in</button></form></html>'
        )
        cookie(response, LOGIN_COOKIE, identifier, 15 * 60)
        return response

    async def form_input(request):
        if (
            request.headers.get("content-type", "").split(";", 1)[0]
            != "application/x-www-form-urlencoded"
        ):
            raise ManagementError("invalid_input")
        try:
            values = parse_qs(
                (await bounded_body(request, 32 * 1024)).decode(), keep_blank_values=True
            )
            if any(len(value) != 1 for value in values.values()):
                raise ValueError
            return {key: value[0] for key, value in values.items()}
        except (ValueError, UnicodeError):
            raise ManagementError("invalid_input") from None

    @app.post("/login")
    async def login(request: Request):
        verify_origin(request)
        values = await form_input(request)
        if set(values) != {"csrf", "username", "password"}:
            raise ManagementError("invalid_input")
        auth = app.state.auth
        identifier = request.cookies.get(LOGIN_COOKIE)
        if not auth.valid_csrf(identifier, values["csrf"], login=True):
            raise ManagementError("csrf_rejected")
        if not auth.allow_attempt(request.client.host if request.client else "unknown"):
            raise ManagementError("login_throttled")
        if not auth.authenticate(values["username"], values["password"]):
            raise ManagementError("invalid_credentials")
        auth.prelogin.pop(identifier, None)
        identifier, _ = auth.issue()
        response = RedirectResponse("/", status_code=303)
        cookie(response, COOKIE, identifier, MAX_AGE)
        response.delete_cookie(
            LOGIN_COOKIE,
            path="/",
            secure=app.state.settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        values = await form_input(request)
        if set(values) != {"csrf"}:
            raise ManagementError("invalid_input")
        session(request, mutation=True, token=values["csrf"])
        app.state.auth.sessions.pop(request.cookies.get(COOKIE), None)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            COOKIE, path="/", secure=app.state.settings.cookie_secure, httponly=True, samesite="lax"
        )
        return response

    @app.get("/")
    @app.get("/sites/{site_id}")
    @app.get("/devices/{device_id}")
    async def placeholder(request: Request):
        try:
            state = session(request)
        except ManagementError as exc:
            if exc.code == "authentication_required":
                return RedirectResponse("/login", status_code=303)
            raise
        return HTMLResponse(
            '<!doctype html><html lang="en"><title>Watt Profile Server</title>'
            "<h1>Watt Profile Server</h1><p>Administrator pages are under construction.</p>"
            f'<form method="post" action="/logout"><input type="hidden" name="csrf" '
            f'value="{escape(state.csrf)}"><button>Log out</button></form></html>'
        )

    async def management(request: Request):
        mutation = request.method not in {"GET", "HEAD"}
        session(request, mutation=mutation)
        path = request.path_params
        ids = {key: query_id(value) for key, value in path.items()}
        repo, queries = app.state.registration, app.state.queries
        method, route = request.method, request.scope["route"].path.removeprefix(PREFIX)
        call = None
        if route in {"/registrations", "/registrations/preview"}:
            summary = await json_input(request, RegistrationSummary)
            call = partial(repo.preview if route.endswith("preview") else repo.commit, summary)
        elif route == "/sites":
            if method == "POST":
                call = partial(repo.create_site, await json_input(request, SiteInput))
            else:
                search = SiteSearch.model_validate(
                    query_values(request, {"site_id", "q", "offset", "limit"})
                )
                search.predicate()
                call = partial(queries.site_page, search)
        elif route == "/sites/{site_id}":
            call = (
                partial(repo.edit_site, ids["site_id"], await json_input(request, SitePatch))
                if mutation
                else partial(queries.site_status, ids["site_id"])
            )
        elif route == "/devices":
            call = (
                partial(repo.create_device, await json_input(request, DeviceInput))
                if mutation
                else partial(
                    queries.device_page,
                    PageOptions.model_validate(query_values(request, {"offset", "limit"})),
                )
            )
        elif route == "/devices/{device_id}":

            def detail():
                return {
                    "status": queries.device_status(ids["device_id"]).model_dump(mode="json"),
                    **repo.get_device(ids["device_id"]).model_dump(mode="json"),
                }

            call = detail
        elif route.endswith("/credentials"):
            value = await json_input(request, CredentialInput)
            call = partial(repo.register_credential, ids["device_id"], value.token)
        elif route.endswith("/disable"):
            await json_input(request, None)
            call = partial(repo.disable_credential, ids["device_id"], ids["credential_id"])
        elif route == "/devices/{device_id}/sites/{site_id}":
            if method == "PUT":
                await json_input(request, None)
                call = partial(repo.grant_site, ids["device_id"], ids["site_id"])
            else:
                if await bounded_body(request, 32 * 1024):
                    raise ManagementError("invalid_input")
                await run_in_threadpool(repo.revoke_site, ids["device_id"], ids["site_id"])
                return Response(status_code=204)
        elif route.endswith("/sessions"):
            values = query_values(request, {"device_id", "offset", "limit"})
            device_id = values.pop("device_id", None)
            call = partial(
                queries.session_page, ids["site_id"], device_id, PageOptions.model_validate(values)
            )
        elif route.endswith("/readings") or route.endswith("/readings.csv"):
            exporting = route.endswith(".csv")
            allowed = {"device_id", "session_id", "received_from", "received_before"}
            values = query_values(request, allowed if exporting else allowed | {"offset", "limit"})
            options = ReadingPageOptions.model_validate(
                {key: values.pop(key) for key in ("offset", "limit") if key in values}
            )
            filters = parse_filters(values)
            if exporting:
                stream = await run_in_threadpool(app.state.exports.open, ids["site_id"], filters)
                return OwnedStreamingResponse(stream)
            call = partial(queries.reading_page, ids["site_id"], filters, options)
        elif "/readings/" in route:
            call = partial(
                queries.snapshot_detail, ids["site_id"], ids["device_id"], ids["record_id"]
            )
        result = await run_in_threadpool(call)
        return JSONResponse(
            {"data": result.model_dump(mode="json") if hasattr(result, "model_dump") else result}
        )

    routes = {
        "/registrations/preview": ["POST"],
        "/registrations": ["POST"],
        "/sites": ["GET", "POST"],
        "/sites/{site_id}": ["GET", "PATCH"],
        "/devices": ["GET", "POST"],
        "/devices/{device_id}": ["GET"],
        "/devices/{device_id}/credentials": ["POST"],
        "/devices/{device_id}/credentials/{credential_id}/disable": ["POST"],
        "/devices/{device_id}/sites/{site_id}": ["PUT", "DELETE"],
        "/sites/{site_id}/sessions": ["GET"],
        "/sites/{site_id}/readings": ["GET"],
        "/sites/{site_id}/readings.csv": ["GET"],
        "/sites/{site_id}/readings/{device_id}/{record_id}": ["GET"],
    }
    for path, methods in routes.items():
        app.add_api_route(PREFIX + path, management, methods=methods)

    @app.post("/api/v1/ingest/batches")
    async def ingest(request: Request):
        if not app.state.ready:
            raise UploadValidationError("temporarily_unavailable")
        body = await bounded_body(request, MAX_BODY_BYTES, ingest=True)
        authorization = request.headers.get("authorization", "")
        pieces = authorization.split(" ")
        token = pieces[1] if len(pieces) == 2 and pieces[0].lower() == "bearer" else None
        credential = await run_in_threadpool(app.state.ingest.authenticate_contact, token)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise UploadValidationError("invalid_payload")
        result = await run_in_threadpool(
            partial(
                app.state.ingest.accept_body,
                credential,
                body,
                content_encoding=request.headers.get("content-encoding"),
            )
        )
        return JSONResponse(result.model_dump(mode="json"))

    return app
