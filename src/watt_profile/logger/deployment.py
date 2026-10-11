"""Typed deployment commands; the runtime serializes calls and supplies draining."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import StrictBool, TypeAdapter

from watt_profile.contracts import lifecycle as pure
from watt_profile.contracts.domain import UUID4, DomainModel, UtcTime, new_id
from watt_profile.logger.storage.repository import LocalSettings, LoggerRepository, Site


class DeploymentCommand(DomainModel):
    site_id: UUID4
    expected_session_id: UUID4 | None
    confirm_switch: StrictBool = False


class SettingsCommand(DomainModel):
    settings: LocalSettings
    expected_session_id: UUID4 | None


Drain = Callable[[pure.Lifecycle], datetime]


class DeploymentOperations:
    """One runtime-owned state; no reader, thread, lock or pending target in SQLite.

    The drain callback cancels/joins the old owner and finalizes its eligible tail,
    returning UTC completion time. It runs after disabled intent commits and outside
    every repository transaction. A failed drain/commit leaves the old owner paused.
    Runtime callers must serialize commands and prevent new owners after failed join.
    """

    def __init__(self, repository: LoggerRepository, drain: Drain):
        self.repository = repository
        self.drain = drain
        self.state = repository.load_lifecycle()
        # Lifecycle transitions need all effective blocks, including failed writes.
        # Only repository state describes which reasons are actually durable.
        self.unsaved_blocks: tuple[pure.PersistedBlock, ...] = ()

    def _retain_unsaved_blocks(self, state: pure.Lifecycle) -> pure.Lifecycle:
        for reason in self.unsaved_blocks:
            state = pure.block_collection(state, reason).lifecycle
        return state

    def _selected(self) -> UUID | None:
        return self.state.session.descriptor.session_id if self.state.session else None

    def _check(self, expected: UUID | None) -> None:
        persisted = self._retain_unsaved_blocks(self.repository.load_lifecycle())
        selected = persisted.session.descriptor.session_id if persisted.session else None
        if expected != selected or expected != self._selected():
            raise ValueError("stale_session")
        # Refresh independently changed blocks/binding without restoring a runtime owner.
        state_name = self.state.collection_state
        if state_name != "stopping":
            if persisted.persisted_blocks:
                state_name = "blocked"
            elif state_name == "blocked":
                state_name = "idle"
        self.state = pure.Lifecycle.model_validate(
            {name: getattr(self.state, name) for name in pure.Lifecycle.model_fields}
            | {
                "persisted_blocks": persisted.persisted_blocks,
                "source": persisted.source,
                "collection_state": state_name,
            }
        )

    def create_site(
        self, *, site_id: UUID | None = None, address: str = "", description: str = ""
    ) -> Site:
        return self.repository.save_site(
            Site(
                site_id=site_id if site_id is not None else new_id(),
                address=address,
                description=description,
            )
        )

    def edit_site(self, site: Site) -> Site:
        return self.repository.save_site(site, edit=True)

    def restore_intent(self) -> pure.Transition:
        self.state = pure.restart(
            self._retain_unsaved_blocks(self.repository.load_lifecycle())
        ).lifecycle
        return pure.Transition(
            lifecycle=self.state, result="blocked" if self.state.persisted_blocks else "ok"
        )

    def start(
        self, expected_session_id: UUID, *, input_mode: Literal["serial", "synthetic"] = "serial"
    ) -> pure.Transition:
        self._check(expected_session_id)
        if input_mode not in {"serial", "synthetic"}:
            raise ValueError("invalid_input_mode")
        result = pure.start_collection(self.state, expected_session_id)
        if result.result == "ok":
            if (
                input_mode == "serial"
                and self.repository.load_installation().settings.serial_path is None
            ):
                raise ValueError("serial_path_required")
            self.repository.commit_deployment_change(expected_session_id, result.lifecycle)
            self.state = result.lifecycle
        return result

    def owner_started(self) -> pure.Transition:
        result = pure.owner_started(self.state)
        self.state = result.lifecycle
        return result

    def _stop(self, result: pure.Transition, now: datetime) -> pure.Transition:
        if result.result != "ok":
            return result
        selected = self._selected()
        self.repository.commit_deployment_change(
            selected, pure.stop_collection(self.state, selected).lifecycle
        )
        self.state = result.lifecycle
        drained = self.state.collection_state != "stopping"
        try:
            if self.state.collection_state == "stopping":
                ended = self.drain(self.state)
                # Validate callback time before any completion is committed.
                ended = TypeAdapter(UtcTime).validate_python(ended)
                drained = True
                result = pure.finish_stop(self.state, ended)
            self.state = self.repository.commit_deployment_change(selected, result.lifecycle)
        except BaseException:
            if drained:
                self.state = self._retain_unsaved_blocks(self.repository.load_lifecycle())
            raise
        return pure.Transition(lifecycle=self.state, result=result.result)

    def pause(self, expected_session_id: UUID, now: datetime) -> pure.Transition:
        self._check(expected_session_id)
        return self._stop(pure.stop_collection(self.state, expected_session_id), now)

    def complete(self, expected_session_id: UUID, now: datetime) -> pure.Transition:
        self._check(expected_session_id)
        return self._stop(pure.complete_session(self.state, expected_session_id, now), now)

    def deploy(self, command: DeploymentCommand, now: datetime) -> pure.Transition:
        command = DeploymentCommand.model_validate(command.model_dump())
        self._check(command.expected_session_id)
        if self.state.session and self.state.session.state == "open":
            if self.state.session.descriptor.site_id == command.site_id:
                raise ValueError("same_site")
            if not command.confirm_switch:
                raise ValueError("switch_confirmation_required")
            return self._stop(pure.switch_site(self.state, command.site_id, now), now)
        result = pure.new_deployment(self.state, command.site_id, now)
        if result.result == "ok":
            self.state = self.repository.commit_deployment_change(
                command.expected_session_id, result.lifecycle
            )
        return pure.Transition(lifecycle=self.state, result=result.result)

    def confirm_meter_change(self, expected_session_id: UUID, now: datetime) -> pure.Transition:
        self._check(expected_session_id)
        result = pure.confirm_meter_change(self.state, now)
        if result.result == "ok":
            self.state = self.repository.commit_deployment_change(
                expected_session_id, result.lifecycle
            )
            self.unsaved_blocks = tuple(
                reason for reason in self.unsaved_blocks if reason != "meter_changed"
            )
        return pure.Transition(lifecycle=self.state, result=result.result)

    def save_settings(self, command: SettingsCommand, now: datetime) -> LocalSettings:
        command = SettingsCommand.model_validate(command.model_dump())
        self._check(command.expected_session_id)
        old = self.repository.load_installation().settings
        serial_names = set(LocalSettings.model_fields) - {"server_url"}
        if any(getattr(old, n) != getattr(command.settings, n) for n in serial_names):
            result = pure.serial_configuration_changed(self.state)
            if result.result != "ok":
                raise ValueError(result.result)
            if self.state.session:
                self._stop(result, now)
        return self.repository.save_settings(
            command.settings, expected_session_id=command.expected_session_id
        )
