"""Pure collection transitions; runtime ownership and draining belong to M2."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from watt_profile.contracts.domain import (
    UUID4,
    UUID5,
    DomainModel,
    SessionDescriptor,
    SourceDescriptor,
    UtcTime,
    measurement_point_id,
    new_id,
)

CollectionState = Literal[
    "idle", "previewing", "starting", "collecting", "reconnecting", "stopping", "blocked"
]
CollectionError = Literal[
    "serial_busy",
    "serial_unavailable",
    "serial_permission",
    "no_valid_reading",
    "meter_changed",
    "storage_full",
]
PersistedBlock = Literal["meter_changed", "storage_full"]
Count = Annotated[StrictInt, Field(ge=0)]


class LocalSession(DomainModel):
    descriptor: SessionDescriptor
    state: Literal["open", "completed"] = "open"
    ended_at: UtcTime | None = None
    collection_enabled: StrictBool = False

    @model_validator(mode="after")
    def completion(self) -> Self:
        if self.state == "open" and self.ended_at is not None:
            raise ValueError("open_session_has_end")
        if self.state == "completed" and (self.ended_at is None or self.collection_enabled):
            raise ValueError("invalid_completed_session")
        return self


class LocalSourceBinding(DomainModel):
    descriptor: SourceDescriptor
    # Only this local comparison binding retains a fingerprint. Never on wire.
    meter_fingerprint: StrictStr | None = Field(default=None, repr=False, exclude=True)

    @model_validator(mode="after")
    def fingerprint(self) -> Self:
        if (
            self.meter_fingerprint is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.meter_fingerprint) is None
        ):
            raise ValueError("invalid_fingerprint")
        return self


class Lifecycle(DomainModel):
    device_id: UUID4
    session: LocalSession | None = None
    source: LocalSourceBinding | None = None
    collection_state: CollectionState = "idle"
    persisted_blocks: tuple[PersistedBlock, ...] = ()
    latest_error: CollectionError | None = None
    stop_action: Literal["pause", "complete", "switch"] | None = None
    next_site_id: UUID4 | None = None

    @model_validator(mode="after")
    def bindings(self) -> Self:
        if len(self.persisted_blocks) != len(set(self.persisted_blocks)):
            raise ValueError("duplicate_block")
        if (self.session is None) != (self.source is None):
            raise ValueError("incomplete_binding")
        if self.session is not None:
            session = self.session.descriptor
            source = self.source.descriptor
            if (
                session.device_id != self.device_id
                or source.session_id != session.session_id
                or source.measurement_point_id != measurement_point_id(session.site_id)
            ):
                raise ValueError("descriptor_mismatch")
        if self.collection_state in {"starting", "collecting", "reconnecting", "stopping"}:
            if self.session is None or self.session.state != "open":
                raise ValueError("open_session_required")
        if self.collection_state in {"starting", "collecting", "reconnecting"}:
            if not self.session.collection_enabled or self.persisted_blocks:
                raise ValueError("collection_intent_or_block_mismatch")
        if self.collection_state == "blocked" and not self.persisted_blocks:
            raise ValueError("block_required")
        if (self.collection_state == "stopping") != (self.stop_action is not None):
            raise ValueError("invalid_stop_action")
        if (self.stop_action == "switch") != (self.next_site_id is not None):
            raise ValueError("invalid_switch_site")
        return self


class Transition(DomainModel):
    lifecycle: Lifecycle
    result: Literal["ok", "busy", "blocked", "completed", "no_session", "subscribed"]


def _replace(model: DomainModel, **changes: object):
    # model_copy deliberately skips validation; every transition instead revalidates.
    values = {name: getattr(model, name) for name in type(model).model_fields}
    return type(model).model_validate(values | changes)


def _result(state: Lifecycle, result: str = "ok", **changes: object) -> Transition:
    return Transition(lifecycle=_replace(state, **changes), result=result)


def new_deployment(state: Lifecycle, site_id: UUID4, now: datetime) -> Transition:
    """Create a paused deployment only after the previous one is completed."""
    if state.collection_state not in {"idle", "blocked"}:
        return _result(state, "busy")
    if state.session is not None and state.session.state == "open":
        return _result(state, "busy")
    session = SessionDescriptor(
        session_id=new_id(), device_id=state.device_id, site_id=site_id, started_at=now
    )
    source = SourceDescriptor(
        source_id=new_id(),
        session_id=session.session_id,
        measurement_point_id=measurement_point_id(site_id),
    )
    return _result(
        state,
        session=LocalSession(descriptor=session),
        source=LocalSourceBinding(descriptor=source),
        collection_state="blocked" if "storage_full" in state.persisted_blocks else "idle",
        persisted_blocks=("storage_full",) if "storage_full" in state.persisted_blocks else (),
        latest_error="storage_full" if "storage_full" in state.persisted_blocks else None,
    )


def start_collection(state: Lifecycle, session_id: UUID4) -> Transition:
    if state.session is None:
        return _result(state, "no_session")
    if state.session.descriptor.session_id != session_id:
        return _result(state, "busy")
    if state.session.state == "completed":
        return _result(state, "completed")
    if state.persisted_blocks:
        return _result(state, "blocked")
    if state.collection_state in {"starting", "collecting", "reconnecting"}:
        return _result(state)
    if state.collection_state != "idle":
        return _result(state, "busy")
    return _result(
        state,
        session=_replace(state.session, collection_enabled=True),
        collection_state="starting",
        latest_error=None,
    )


def owner_started(state: Lifecycle) -> Transition:
    if state.collection_state not in {"starting", "reconnecting"}:
        return _result(state, "busy")
    return _result(state, collection_state="collecting", latest_error=None)


def stop_collection(state: Lifecycle, session_id: UUID4) -> Transition:
    if state.session is None:
        return _result(state, "no_session")
    if state.session.descriptor.session_id != session_id:
        return _result(state, "busy")
    if state.collection_state == "stopping":
        return _result(state, "ok" if state.stop_action == "pause" else "busy")
    if state.collection_state == "previewing":
        return _result(state, "busy")
    session = _replace(state.session, collection_enabled=False)
    if state.collection_state in {"idle", "blocked"}:
        return _result(state, session=session)
    return _result(state, session=session, collection_state="stopping", stop_action="pause")


def complete_session(state: Lifecycle, session_id: UUID4, now: datetime) -> Transition:
    if state.session is None:
        return _result(state, "no_session")
    if state.session.descriptor.session_id != session_id:
        return _result(state, "busy")
    if state.session.state == "completed":
        return _result(state)
    if state.collection_state in {"stopping", "previewing"}:
        return _result(state, "busy")
    if state.collection_state in {"idle", "blocked"}:
        return _result(
            state,
            session=_replace(
                state.session, state="completed", ended_at=now, collection_enabled=False
            ),
            collection_state="blocked" if "storage_full" in state.persisted_blocks else "idle",
            persisted_blocks=("storage_full",) if "storage_full" in state.persisted_blocks else (),
            latest_error="storage_full" if "storage_full" in state.persisted_blocks else None,
        )
    return _result(
        state,
        collection_state="stopping",
        stop_action="complete",
        session=_replace(state.session, collection_enabled=False),
    )


def switch_site(state: Lifecycle, site_id: UUID4, now: datetime) -> Transition:
    if state.collection_state in {"stopping", "previewing"}:
        return _result(state, "busy")
    if state.session is None or state.session.state == "completed":
        return new_deployment(state, site_id, now)
    if state.collection_state in {"idle", "blocked"}:
        closed = complete_session(state, state.session.descriptor.session_id, now)
        return new_deployment(closed.lifecycle, site_id, now)
    return _result(
        state,
        collection_state="stopping",
        stop_action="switch",
        next_site_id=site_id,
        session=_replace(state.session, collection_enabled=False),
    )


def finish_stop(state: Lifecycle, now: datetime) -> Transition:
    """Call only after reader exit and scheduled final-window drain; no I/O here."""
    if state.collection_state != "stopping":
        return _result(state, "busy")
    action, site = state.stop_action, state.next_site_id
    idle = _replace(state, collection_state="idle", stop_action=None, next_site_id=None)
    if action == "pause":
        return _result(idle)
    closed = complete_session(idle, idle.session.descriptor.session_id, now)
    return new_deployment(closed.lifecycle, site, now) if action == "switch" else closed


def preview(state: Lifecycle) -> Transition:
    if state.collection_state in {"collecting", "reconnecting"}:
        return _result(state, "subscribed")
    if state.collection_state != "idle":
        return _result(state, "busy")
    return _result(state, collection_state="previewing")


def finish_preview(state: Lifecycle, error: CollectionError | None = None) -> Transition:
    if state.collection_state != "previewing":
        return _result(state, "busy")
    return _result(state, collection_state="idle", latest_error=error)


def restart(state: Lifecycle) -> Transition:
    """Rebuild runtime intent from persisted state without inheriting old owners."""
    idle = _replace(
        state,
        collection_state="blocked" if state.persisted_blocks else "idle",
        stop_action=None,
        next_site_id=None,
    )
    if idle.persisted_blocks:
        return _result(idle, "blocked")
    if idle.session and idle.session.state == "open" and idle.session.collection_enabled:
        return start_collection(idle, idle.session.descriptor.session_id)
    return _result(idle)


def block_collection(state: Lifecycle, code: PersistedBlock) -> Transition:
    """Record a block; the runtime must release its reader before further operations."""
    return _result(
        state,
        collection_state="blocked",
        persisted_blocks=(
            state.persisted_blocks
            if code in state.persisted_blocks
            else (*state.persisted_blocks, code)
        ),
        latest_error=code,
        stop_action=None,
        next_site_id=None,
    )


def resolve_storage_block(state: Lifecycle) -> Transition:
    if "storage_full" not in state.persisted_blocks:
        return _result(state, "blocked" if state.persisted_blocks else "ok")
    # Resolution deliberately pauses; an explicit start is still necessary.
    session = _replace(state.session, collection_enabled=False) if state.session else None
    remaining = tuple(code for code in state.persisted_blocks if code != "storage_full")
    return _result(
        state,
        "blocked" if remaining else "ok",
        collection_state="blocked" if remaining else "idle",
        persisted_blocks=remaining,
        latest_error=remaining[-1] if remaining else None,
        session=session,
    )


def observe_meter(state: Lifecycle, fingerprint: str | None) -> Transition:
    if state.source is None:
        return _result(state, "no_session")
    if fingerprint is None:
        return _result(state)
    candidate = LocalSourceBinding(
        descriptor=state.source.descriptor, meter_fingerprint=fingerprint
    )
    if state.source.meter_fingerprint is None:
        return _result(state, source=candidate)
    if state.source.meter_fingerprint != fingerprint:
        return block_collection(state, "meter_changed")
    return _result(state)


def confirm_meter_change(state: Lifecycle, now: datetime) -> Transition:
    if "meter_changed" not in state.persisted_blocks or state.session is None:
        return _result(state, "busy")
    site = state.session.descriptor.site_id
    closed = complete_session(state, state.session.descriptor.session_id, now)
    return new_deployment(closed.lifecycle, site, now)


def serial_configuration_changed(state: Lifecycle) -> Transition:
    if state.session is None:
        return _result(state, "busy" if state.collection_state == "previewing" else "ok")
    return stop_collection(state, state.session.descriptor.session_id)


def disconnected(state: Lifecycle, error: CollectionError = "serial_unavailable") -> Transition:
    if state.collection_state != "collecting":
        return _result(state, "busy")
    return _result(state, collection_state="reconnecting", latest_error=error)


class LoggerStatus(DomainModel):
    collection_state: CollectionState
    device_id: UUID4 | None
    site_id: UUID4 | None
    session_id: UUID4 | None
    source_id: UUID4 | None
    measurement_point_id: UUID5 | None
    latest_received_at: UtcTime | None
    latest_collection_error: CollectionError | None
    last_successful_upload_at: UtcTime | None
    last_upload_error: StrictStr | None
    pending_count: Count
    pending_bytes: Count
    oldest_pending_received_at: UtcTime | None
    free_bytes: Count
    storage_blocked: StrictBool
