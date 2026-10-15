"""Authenticated local HTTP adapter; collection commands run in worker threads."""

from __future__ import annotations

import asyncio
import threading
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import Field, StrictBool, StrictInt, StrictStr, ValidationError, model_validator
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from watt_profile.contracts.domain import UUID4, UUID5, DomainModel, UtcTime
from watt_profile.contracts.lifecycle import LoggerStatus, PersistedBlock
from watt_profile.logger.auth import COOKIE, LOGIN_COOKIE, MAX_AGE, OperatorAuth
from watt_profile.logger.bootstrap import installation_lock, set_private_umask
from watt_profile.logger.deployment import DeploymentCommand, SettingsEdit, SiteEdit
from watt_profile.logger.identity import load_or_initialize_installation, read_upload_token
from watt_profile.logger.runtime import LoggerRuntime, ReadingView
from watt_profile.logger.settings import LoggerSettings
from watt_profile.logger.storage.database import create_database, migrate
from watt_profile.logger.storage.repository import LocalSettings, LoggerRepository, Site
from watt_profile.logger.synthetic import SyntheticSerialFactory
from watt_profile.logger.upload import UploadScheduler, UploadTransport

PREFIX = "/api/local/v1"


class EmptyCommand(DomainModel):
    pass


class DataEnvelope[T](DomainModel):
    data: T


class SiteCreate(DomainModel):
    site_id: UUID4 | None = None
    address: Annotated[StrictStr, Field(max_length=500)] = ""
    description: Annotated[StrictStr, Field(max_length=2000)] = ""


class SelectionCommand(DomainModel):
    expected_session_id: UUID4 | None


class RegistrationCommand(DomainModel):
    site_id: UUID4


class CleanupCommand(DomainModel):
    session_ids: Annotated[tuple[UUID4, ...], Field(min_length=1, max_length=100)]
    confirm: StrictBool

    @model_validator(mode="after")
    def confirmation(self):
        if self.confirm is not True or len(set(self.session_ids)) != len(self.session_ids):
            raise ValueError("invalid_cleanup")
        return self


class UploadObservationView(DomainModel):
    state: Literal["idle", "sending", "backoff", "blocked", "stopping", "failed"]
    next_attempt_at: UtcTime | None
    global_error: StrictStr | None
    blocked_group_count: StrictInt


class StatusView(DomainModel):
    logger: LoggerStatus
    input_mode: Literal["serial", "synthetic"]
    upload_mode: Literal["enabled", "unconfigured"]
    persisted_blocks: tuple[PersistedBlock, ...]
    active_session_state: Literal["open", "completed"] | None
    latest_live_reading: ReadingView | None
    active_session_latest_received_at: UtcTime | None
    runtime_error: StrictStr | None
    upload: UploadObservationView


class SessionView(DomainModel):
    session_id: UUID4
    device_id: UUID4
    site_id: UUID4
    started_at: UtcTime
    source_id: UUID4
    measurement_point_id: UUID5
    state: Literal["open", "completed"]
    collection_enabled: StrictBool
    ended_at: UtcTime | None
    total_count: StrictInt
    pending_count: StrictInt
    eligible_uploaded_count: StrictInt
    upload_error: StrictStr | None = None


class SitePage(DomainModel):
    items: tuple[Site, ...]
    total: StrictInt


class SessionPage(DomainModel):
    items: tuple[SessionView, ...]
    total: StrictInt


class RegistrationView(DomainModel):
    site_id: UUID4
    address: StrictStr
    description: StrictStr
    device_id: UUID4
    token: StrictStr


class CleanupView(DomainModel):
    deleted_count: StrictInt
    retained_pending_count: StrictInt
    free_bytes: StrictInt


class ApiError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status


def error(code, status):
    messages = {
        "unauthorized": "Please log in.",
        "forbidden": "Request verification failed.",
        "validation_error": "Invalid request.",
        "not_found": "Object not found.",
        "rate_limited": "Too many login attempts. Try again later.",
        "storage_error": "Storage operation failed.",
        "unavailable": "Service unavailable.",
        "serial_close_failed": (
            "The serial device could not be released. Serial operations are unavailable; "
            "restart the Logger to recover."
        ),
        "serial_join_failed": (
            "The serial reader could not be stopped. Serial operations are unavailable; "
            "restart the Logger to recover."
        ),
    }
    return JSONResponse(
        {"error": {"code": code, "message": messages.get(code, "Command cannot be completed.")}},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def data(value):
    return {"data": value}


def same_origin(request):
    origin = request.headers.get("origin")
    if origin is None:
        return True
    try:
        supplied = urlsplit(origin)
        actual = urlsplit(str(request.base_url))

        def authority(url):
            return url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80)

        return (
            supplied.scheme in {"http", "https"}
            and supplied.hostname is not None
            and not supplied.username
            and not supplied.password
            and not supplied.path
            and not supplied.query
            and not supplied.fragment
            and authority(supplied) == authority(actual)
        )
    except ValueError:
        return False


async def form_values(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            raise ApiError("validation_error", 422)
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise ApiError("validation_error", 422)
    try:
        values = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=4)
    except (UnicodeError, ValueError):
        raise ApiError("validation_error", 422) from None
    if any(len(v) != 1 for v in values.values()):
        raise ApiError("validation_error", 422)
    return {k: v[0] for k, v in values.items()}


async def finish_thread(function, *args):
    """Wait for started database/runtime work even if its caller is cancelled."""
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await asyncio.shield(operation)
        raise


def create_app(
    settings: LoggerSettings,
    *,
    serial_factory=None,
    clock=None,
    auth_clock=None,
    upload_transport_factory=None,
    upload_scheduler_factory=None,
    upload_clock=None,
):
    """Create without side effects. Lifespan owns the installation lock and resources."""
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

    @asynccontextmanager
    async def lifespan(app):
        stack = ExitStack()
        app.state.ready = False
        app.state.auth = None
        app.state.runtime = None
        app.state.upload_scheduler = None
        app.state.upload_task = None
        app.state.upload_worker_error = None
        app.state.upload_stopping = False
        app.state.upload_transport = None

        def initialize():
            set_private_umask()
            stack.enter_context(installation_lock(settings.data_dir))
            engine = create_database(settings.data_dir / "logger.sqlite3")
            stack.callback(engine.dispose)
            migrate(engine)
            repository = LoggerRepository(engine)
            load_or_initialize_installation(repository, settings.data_dir, datetime.now(UTC))
            password = settings.operator_password_file.read_text(encoding="utf-8")
            if password.endswith("\n"):
                password = password[:-1]
            if not 16 <= len(password) <= 256 or "\x00" in password:
                raise RuntimeError("invalid_operator_password")
            app.state.auth = OperatorAuth(
                settings.operator_user,
                password,
                **({"clock": auth_clock} if auth_clock is not None else {}),
            )
            runtime = LoggerRuntime(
                repository,
                serial_factory=(
                    SyntheticSerialFactory()
                    if settings.input_mode == "synthetic" and serial_factory is None
                    else serial_factory
                ),
                clock=clock,
                input_mode=settings.input_mode,
            )
            app.state.repository = repository
            app.state.runtime = runtime
            runtime.restore_intent()

        try:
            await finish_thread(initialize)
            app.state.loop = asyncio.get_running_loop()
            transport = (upload_transport_factory or UploadTransport)(settings)
            app.state.upload_transport = transport

            async def upload_fatal(code):
                if code == "storage_error" and app.state.runtime is not None:
                    await finish_thread(app.state.runtime.mark_external_storage_fatal)

            scheduler_args = {}
            if upload_clock is not None:
                scheduler_args.update(monotonic=upload_clock.monotonic, utcnow=upload_clock.utcnow)
            scheduler = (upload_scheduler_factory or UploadScheduler)(
                app.state.repository,
                transport,
                lambda: read_upload_token(settings.data_dir),
                on_fatal=upload_fatal,
                **scheduler_args,
            )
            app.state.upload_scheduler = scheduler

            async def run_upload():
                try:
                    await scheduler.run()
                except asyncio.CancelledError:
                    if not app.state.upload_stopping:
                        app.state.upload_worker_error = "upload_worker_failed"
                    raise
                except Exception:
                    app.state.upload_worker_error = "upload_worker_failed"
                else:
                    if not app.state.upload_stopping and not getattr(scheduler, "_fatal", False):
                        app.state.upload_worker_error = "upload_worker_failed"

            app.state.upload_task = asyncio.create_task(run_upload(), name="logger-upload")
            app.state.ready = True
            yield
        finally:
            app.state.ready = False
            app.state.upload_stopping = True
            scheduler = app.state.upload_scheduler
            worker = app.state.upload_task
            if scheduler is not None:
                scheduler.stop()
            if worker is not None:
                if not worker.done():
                    worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            transport = getattr(app.state, "upload_transport", None)
            try:
                if transport is not None:
                    await transport.aclose()
            finally:
                if app.state.auth:
                    app.state.auth.clear()
                runtime = app.state.runtime
                try:
                    if runtime is not None:
                        # Let an already admitted command finish before the shutdown drain.
                        while True:
                            try:
                                await finish_thread(runtime.shutdown)
                                break
                            except ValueError as exc:
                                if exc.args != ("busy",):
                                    raise
                                await asyncio.sleep(0.05)
                finally:
                    stack.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ready = False
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.middleware("http")
    async def security(request, call_next):
        private = not request.url.path.startswith("/health/")
        if not same_origin(request):
            return error("forbidden", 403)
        if request.url.path.startswith(PREFIX):
            auth = getattr(app.state, "auth", None)
            identifier = request.cookies.get(COOKIE)
            state = auth.session(identifier) if auth else None
            if not state:
                return error("unauthorized", 401)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not auth.valid_csrf(
                identifier, request.headers.get("X-CSRF-Token")
            ):
                return error("forbidden", 403)
        response = await call_next(request)
        if private:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return error(exc.code, exc.status)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def invalid(request, exc):
        return error("validation_error", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error("not_found" if exc.status_code == 404 else "validation_error", exc.status_code)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return error("not_found", 404)

    @app.exception_handler(ValueError)
    async def command_error(request, exc):
        code = exc.args[0] if exc.args else "conflict"
        conflicts = {
            "busy",
            "blocked",
            "stale_session",
            "pending_uploads_prevent_server_change",
            "same_site",
            "switch_confirmation_required",
            "session_completed",
            "no_session",
            "meter_change_required",
            "runtime_closed",
            "invalid_state",
            "rejected",
            "serial_close_failed",
            "serial_join_failed",
            "preview_cancelled",
        }
        unavailable = {
            "serial_unavailable",
            "serial_permission",
            "serial_busy",
            "serial_path_required",
            "preview_timeout",
            "no_valid_reading",
        }
        if code in unavailable:
            return error(code, 503)
        return error(code if code in conflicts else "conflict", 409)

    @app.exception_handler(OSError)
    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(RuntimeError)
    async def storage_error(request, exc):
        return error("storage_error", 507)

    def status():
        result = app.state.runtime.get_status()
        installation = app.state.repository.load_installation()
        result["upload_mode"] = "enabled" if installation.settings.server_url else "unconfigured"
        scheduler = app.state.upload_scheduler
        if scheduler is not None:
            observation = scheduler.observation
            worker_failed = app.state.upload_worker_error is not None
            result["logger"] = result["logger"].model_copy(
                update={
                    "last_upload_error": app.state.upload_worker_error or observation.global_error
                }
            )
            result["upload"] = UploadObservationView(
                state="failed" if worker_failed else observation.state,
                next_attempt_at=observation.next_attempt_at,
                global_error=app.state.upload_worker_error or observation.global_error,
                blocked_group_count=observation.blocked_group_count,
            )
        else:
            result["upload"] = UploadObservationView(
                state="failed" if app.state.upload_worker_error else "idle",
                next_attempt_at=None,
                global_error=app.state.upload_worker_error,
                blocked_group_count=0,
            )
        return StatusView.model_validate(result)

    def final(result):
        if result.result not in {"ok", "already_started", "already_stopped", "already_completed"}:
            raise ApiError("blocked" if result.result == "blocked" else "conflict", 409)
        return data(status())

    @app.get("/health/live")
    async def live():
        return {"live": True}

    @app.get("/health/ready")
    def ready():
        healthy = app.state.ready
        if healthy:
            try:
                app.state.repository.load_installation()
                worker = app.state.upload_task
                healthy = not app.state.runtime._fatal_storage and not (
                    app.state.upload_worker_error is not None
                    or (worker is not None and worker.done() and not app.state.upload_stopping)
                )
            except Exception:
                healthy = False
        return JSONResponse({"ready": healthy}, status_code=200 if healthy else 503)

    @app.get("/login")
    async def login_page(request: Request):
        identifier, state = app.state.auth.login_state()
        response = templates.TemplateResponse(
            request=request, name="login.html", context={"csrf_token": state.csrf, "error": None}
        )
        response.set_cookie(
            LOGIN_COOKIE,
            identifier,
            httponly=True,
            samesite="lax",
            path="/",
            secure=settings.cookie_secure,
            max_age=900,
        )
        return response

    @app.post("/login")
    async def login(request: Request):
        values = await form_values(request)
        if set(values) != {"username", "password", "csrf_token"}:
            raise ApiError("validation_error", 422)
        auth = app.state.auth
        prelogin = request.cookies.get(LOGIN_COOKIE)
        if not auth.valid_csrf(prelogin, values["csrf_token"], login=True):
            raise ApiError("forbidden", 403)
        address = request.client.host if request.client else "unknown"
        if not auth.allow_attempt(address):
            raise ApiError("rate_limited", 429)
        if not auth.authenticate(values["username"], values["password"]):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                status_code=401,
                context={"csrf_token": values["csrf_token"], "error": "Login failed."},
            )
        auth.prelogin.pop(prelogin, None)
        auth.sessions.pop(request.cookies.get(COOKIE), None)
        identifier, _ = auth.issue()
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(LOGIN_COOKIE, path="/")
        response.set_cookie(
            COOKIE,
            identifier,
            httponly=True,
            samesite="lax",
            path="/",
            secure=settings.cookie_secure,
            max_age=MAX_AGE,
        )
        return response

    @app.get("/")
    async def index(request: Request):
        session = app.state.auth.session(request.cookies.get(COOKIE))
        if not session:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="index.html", context={"csrf_token": session.csrf}
        )

    @app.post("/logout")
    async def logout(request: Request):
        identifier = request.cookies.get(COOKIE)
        if not app.state.auth.session(identifier):
            return RedirectResponse("/login", status_code=303)
        values = await form_values(request)
        if not app.state.auth.valid_csrf(identifier, values.get("csrf_token")):
            raise ApiError("forbidden", 403)
        app.state.auth.sessions.pop(identifier, None)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get(PREFIX + "/status", response_model=DataEnvelope[StatusView])
    def get_status():
        return data(status())

    @app.get(PREFIX + "/config", response_model=DataEnvelope[LocalSettings])
    def config():
        return data(app.state.repository.load_installation().settings)

    @app.patch(PREFIX + "/config", response_model=DataEnvelope[LocalSettings])
    def save_config(command: SettingsEdit):
        result = app.state.runtime.patch_settings(command)
        app.state.loop.call_soon_threadsafe(app.state.upload_scheduler.wake)
        return data(result)

    @app.post(PREFIX + "/upload/retry", status_code=202, response_model=DataEnvelope[StatusView])
    async def retry_upload(command: EmptyCommand):
        app.state.loop.call_soon(app.state.upload_scheduler.retry)
        return data(await asyncio.to_thread(status))

    @app.get(PREFIX + "/sites", response_model=DataEnvelope[SitePage])
    def sites(
        offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 50
    ):
        return data(SitePage.model_validate(app.state.repository.page_sites(offset, limit)))

    @app.post(PREFIX + "/sites", response_model=DataEnvelope[Site])
    def create_site(command: SiteCreate):
        return data(app.state.runtime.create_site(**command.model_dump()))

    @app.patch(PREFIX + "/sites/{site_id}", response_model=DataEnvelope[Site])
    def edit_site(site_id: UUID4, command: SiteEdit):
        return data(app.state.runtime.patch_site(site_id, command))

    @app.post(PREFIX + "/deployment", response_model=DataEnvelope[StatusView])
    def deploy(command: DeploymentCommand):
        app.state.repository.get_site(command.site_id)
        return final(app.state.runtime.switch_site(command))

    @app.post(PREFIX + "/sessions/{session_id}/start", response_model=DataEnvelope[StatusView])
    def start(session_id: UUID4, command: Annotated[EmptyCommand, Body()]):
        app.state.repository.get_session(session_id)
        return final(app.state.runtime.start_collection(session_id))

    @app.post(PREFIX + "/sessions/{session_id}/pause", response_model=DataEnvelope[StatusView])
    def pause(session_id: UUID4, command: Annotated[EmptyCommand, Body()]):
        app.state.repository.get_session(session_id)
        return final(app.state.runtime.stop_collection(session_id))

    @app.post(PREFIX + "/sessions/{session_id}/complete", response_model=DataEnvelope[StatusView])
    def complete(session_id: UUID4, command: Annotated[EmptyCommand, Body()]):
        app.state.repository.get_session(session_id)
        return final(app.state.runtime.complete_session(session_id))

    @app.post(PREFIX + "/preview", response_model=DataEnvelope[ReadingView])
    async def preview(request: Request, command: EmptyCommand):
        cancel = threading.Event()

        async def disconnected():
            while not cancel.is_set():
                if await request.is_disconnected():
                    cancel.set()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(disconnected())
        try:
            result = await asyncio.to_thread(app.state.runtime.preview, cancellation=cancel)
            return data(result)
        finally:
            cancel.set()
            watcher.cancel()

    @app.post(PREFIX + "/meter-change/confirm", response_model=DataEnvelope[StatusView])
    def meter_change(command: SelectionCommand):
        return final(app.state.runtime.confirm_meter_change(command.expected_session_id))

    @app.post(PREFIX + "/storage/resolve", response_model=DataEnvelope[StatusView])
    def resolve(command: EmptyCommand):
        return final(app.state.runtime.resolve_storage_block())

    @app.post(PREFIX + "/registration-summary", response_model=DataEnvelope[RegistrationView])
    def registration(command: RegistrationCommand):
        site = app.state.repository.get_site(command.site_id)
        return data(
            RegistrationView(
                **site.model_dump(),
                device_id=app.state.repository.load_installation().device_id,
                token=read_upload_token(settings.data_dir),
            )
        )

    @app.get(PREFIX + "/sessions", response_model=DataEnvelope[SessionPage])
    def sessions(
        offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 50
    ):
        scheduler = app.state.upload_scheduler
        blocked = (
            {
                tuple(str(part) for part in key): value
                for key, value in scheduler.blocked_group_errors.items()
            }
            if scheduler is not None
            else {}
        )
        page = app.state.repository.page_sessions(offset, limit)
        for item in page["items"]:
            key = (
                item["device_id"],
                item["site_id"],
                item["session_id"],
                item["source_id"],
                item["measurement_point_id"],
            )
            item["upload_error"] = blocked.get(tuple(str(part) for part in key))
        return data(SessionPage.model_validate(page))

    @app.post(PREFIX + "/cleanup", response_model=DataEnvelope[CleanupView])
    def cleanup(command: CleanupCommand):
        result = app.state.runtime.cleanup_completed(command.session_ids)
        retained = sum(
            row["pending_count"]
            for row in app.state.repository.cleanup_summary()
            if row["session_id"] in command.session_ids
        )
        return data(CleanupView(**result, retained_pending_count=retained))

    return app
