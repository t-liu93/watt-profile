"""Pure lifecycle tests do not open ports, store data or launch threads."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from watt_profile.contracts.lifecycle import (
    Lifecycle,
    LocalSession,
    LocalSourceBinding,
    block_collection,
    complete_session,
    confirm_meter_change,
    disconnected,
    finish_preview,
    finish_stop,
    new_deployment,
    observe_meter,
    owner_started,
    preview,
    resolve_storage_block,
    restart,
    serial_configuration_changed,
    start_collection,
    stop_collection,
    switch_site,
)

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def deployment():
    return new_deployment(Lifecycle(device_id=uuid4()), uuid4(), NOW).lifecycle


def collecting():
    state = deployment()
    return owner_started(
        start_collection(state, state.session.descriptor.session_id).lifecycle
    ).lifecycle


def test_pause_resume_restart_and_idempotence():
    state = collecting()
    session_id = state.session.descriptor.session_id
    assert start_collection(state, session_id).lifecycle == state
    assert start_collection(state, uuid4()).result == "busy"
    stopping = stop_collection(state, session_id).lifecycle
    assert stopping.collection_state == "stopping"
    assert start_collection(stopping, session_id).result == "busy"
    assert stop_collection(stopping, session_id).lifecycle == stopping
    idle = finish_stop(stopping, NOW).lifecycle
    assert stop_collection(idle, session_id).lifecycle == idle
    assert restart(idle).lifecycle.collection_state == "idle"
    resumed = start_collection(idle, session_id).lifecycle
    assert resumed.session.descriptor == state.session.descriptor
    assert resumed.source.descriptor == state.source.descriptor
    assert restart(resumed).lifecycle.collection_state == "starting"
    assert restart(stopping).lifecycle.collection_state == "idle"


def test_completion_and_new_existing_site():
    state = collecting()
    session_id = state.session.descriptor.session_id
    stopping = complete_session(state, session_id, NOW).lifecycle
    assert stopping.stop_action == "complete"
    assert stop_collection(stopping, session_id).result == "busy"
    closed = finish_stop(stopping, NOW).lifecycle
    assert closed.session.state == "completed"
    assert closed.session.ended_at == NOW
    assert start_collection(closed, session_id).result == "completed"
    assert complete_session(closed, session_id, NOW).lifecycle == closed
    assert restart(closed).lifecycle.collection_state == "idle"
    newer = new_deployment(closed, state.session.descriptor.site_id, NOW).lifecycle
    assert (
        newer.source.descriptor.measurement_point_id == state.source.descriptor.measurement_point_id
    )
    assert newer.source.descriptor.source_id != state.source.descriptor.source_id
    assert newer.session.descriptor.session_id != session_id
    assert newer.device_id == state.device_id


def test_switch_waits_for_exit_and_changes_ownership():
    state = collecting()
    site_id = uuid4()
    stopping = switch_site(state, site_id, NOW).lifecycle
    assert stopping.session.descriptor == state.session.descriptor
    assert preview(stopping).result == "busy"
    assert switch_site(stopping, site_id, NOW).result == "busy"
    newer = finish_stop(stopping, NOW).lifecycle
    assert newer.session.descriptor.site_id == site_id
    assert newer.device_id == state.device_id
    assert (
        newer.source.descriptor.measurement_point_id != state.source.descriptor.measurement_point_id
    )
    assert newer.source.descriptor.source_id != state.source.descriptor.source_id
    assert newer.collection_state == "idle"


def test_preview_never_creates_session_and_subscribes_to_owner():
    empty = Lifecycle(device_id=uuid4())
    temporary = preview(empty).lifecycle
    assert temporary.session is None and temporary.source is None
    assert new_deployment(temporary, uuid4(), NOW).result == "busy"
    assert (
        finish_preview(temporary, "no_valid_reading").lifecycle.latest_error == "no_valid_reading"
    )
    state = collecting()
    assert preview(state).result == "subscribed"
    assert preview(state).lifecycle == state
    reconnecting = disconnected(state).lifecycle
    assert reconnecting.collection_state == "reconnecting"
    assert owner_started(reconnecting).lifecycle.collection_state == "collecting"
    idle = deployment()
    starting = start_collection(idle, idle.session.descriptor.session_id).lifecycle
    assert preview(starting).result == "busy"


def test_meter_binding_missing_change_confirmation_and_privacy():
    state = collecting()
    assert observe_meter(state, None).lifecycle == state
    bound = observe_meter(state, "a" * 64).lifecycle
    assert observe_meter(bound, None).lifecycle == bound
    assert observe_meter(bound, "a" * 64).lifecycle == bound
    assert "a" * 64 not in repr(bound) and "a" * 64 not in bound.model_dump_json()
    changed = observe_meter(bound, "b" * 64).lifecycle
    assert changed.persisted_blocks == ("meter_changed",)
    assert changed.source.meter_fingerprint == "a" * 64
    assert restart(changed).result == "blocked"
    assert start_collection(changed, changed.session.descriptor.session_id).result == "blocked"
    assert resolve_storage_block(changed).result == "blocked"
    newer = confirm_meter_change(changed, NOW).lifecycle
    assert newer.session.descriptor.site_id == state.session.descriptor.site_id
    assert newer.source.descriptor.source_id != state.source.descriptor.source_id
    assert newer.source.meter_fingerprint is None
    assert newer.persisted_blocks == () and newer.collection_state == "idle"


def test_storage_block_persists_and_resolution_requires_start():
    state = collecting()
    blocked = block_collection(state, "storage_full").lifecycle
    assert restart(blocked).result == "blocked"
    paused = resolve_storage_block(blocked).lifecycle
    assert paused.collection_state == "idle" and not paused.session.collection_enabled
    assert restart(paused).lifecycle.collection_state == "idle"
    assert (
        start_collection(paused, paused.session.descriptor.session_id).lifecycle.collection_state
        == "starting"
    )


def test_serial_configuration_pause_preserves_binding():
    state = collecting()
    stopping = serial_configuration_changed(state).lifecycle
    assert stopping.collection_state == "stopping"
    assert stopping.session.descriptor == state.session.descriptor
    assert stopping.source.descriptor == state.source.descriptor
    assert finish_stop(stopping, NOW).lifecycle.collection_state == "idle"


def test_invalid_persisted_state_and_binding_rejected():
    state = deployment()
    with pytest.raises(ValidationError):
        LocalSession(descriptor=state.session.descriptor, state="completed")
    with pytest.raises(ValidationError):
        LocalSession(descriptor=state.session.descriptor, collection_enabled=1)
    with pytest.raises(ValidationError):
        LocalSourceBinding(descriptor=state.source.descriptor, meter_fingerprint="unsafe")
    with pytest.raises(ValidationError):
        Lifecycle(device_id=uuid4(), session=state.session, source=state.source)
    with pytest.raises(ValidationError):
        Lifecycle(device_id=uuid4(), collection_state="stopping", stop_action="pause")


def test_storage_pressure_survives_site_switch_and_new_session():
    state = block_collection(collecting(), "storage_full").lifecycle
    switched = switch_site(state, uuid4(), NOW).lifecycle
    assert switched.persisted_blocks == ("storage_full",)
    assert restart(switched).result == "blocked"
    completed = complete_session(state, state.session.descriptor.session_id, NOW).lifecycle
    newer = new_deployment(completed, uuid4(), NOW).lifecycle
    assert newer.persisted_blocks == ("storage_full",)


@pytest.mark.parametrize("storage_first", [False, True])
@pytest.mark.parametrize("resolve_storage_first", [False, True])
@pytest.mark.parametrize("reload_and_restart", [False, True])
def test_independent_blocks_survive_each_resolution_and_restart(
    storage_first, resolve_storage_first, reload_and_restart
):
    original = observe_meter(collecting(), "a" * 64).lifecycle
    state = original
    if storage_first:
        state = block_collection(state, "storage_full").lifecycle
    state = observe_meter(state, "b" * 64).lifecycle
    if not storage_first:
        state = block_collection(state, "storage_full").lifecycle
    assert set(state.persisted_blocks) == {"meter_changed", "storage_full"}
    assert (
        block_collection(state, "storage_full").lifecycle.persisted_blocks == state.persisted_blocks
    )

    def restored(blocked):
        if reload_and_restart:
            blocked = Lifecycle.model_validate_json(blocked.model_dump_json())
            transition = restart(blocked)
            assert transition.result == "blocked"
            blocked = transition.lifecycle
        assert blocked.collection_state == "blocked"
        assert start_collection(blocked, blocked.session.descriptor.session_id).result == "blocked"
        return blocked

    state = restored(state)
    if resolve_storage_first:
        resolution = resolve_storage_block(state)
        assert resolution.result == "blocked"
        state = restored(resolution.lifecycle)
        assert state.persisted_blocks == ("meter_changed",)
        assert state.session.descriptor == original.session.descriptor
        assert state.source.descriptor == original.source.descriptor
        state = confirm_meter_change(state, NOW).lifecycle
    else:
        state = restored(confirm_meter_change(state, NOW).lifecycle)
        assert state.persisted_blocks == ("storage_full",)
        assert state.session.descriptor.session_id != original.session.descriptor.session_id
        assert state.source.descriptor.source_id != original.source.descriptor.source_id
        state = resolve_storage_block(state).lifecycle
    assert state.persisted_blocks == ()
    assert state.collection_state == "idle"
    assert not state.session.collection_enabled
    assert state.session.descriptor.session_id != original.session.descriptor.session_id
    assert state.source.descriptor.source_id != original.source.descriptor.source_id
    assert restart(state).lifecycle.collection_state == "idle"
    assert (
        start_collection(state, state.session.descriptor.session_id).lifecycle.collection_state
        == "starting"
    )
