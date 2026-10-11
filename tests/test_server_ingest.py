"""M3-04 atomic ingest/contact contract with independent real PostgreSQL connections."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event, local
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import OperationalError

from watt_profile.contracts.domain import canonical_snapshot_bytes, measurement_point_id
from watt_profile.contracts.upload import (
    UploadBatch,
    UploadValidationError,
    validate_confirmation_body,
)
from watt_profile.server.ingest import IngestService
from watt_profile.server.registration import RegistrationSummary
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.ingest import IngestRepository
from watt_profile.server.storage.registration import RegistrationRepository
from watt_profile.server.storage.repository import acquire_mutation_locks

TOKEN = "a" * 43


@pytest.fixture
def setup(server_engine):
    registration = RegistrationRepository(server_engine)
    summary = RegistrationSummary(
        device_id=uuid4(), site_id=uuid4(), address="synthetic", description="", token=TOKEN
    )
    registered = registration.commit(summary)
    repository = IngestRepository(server_engine)
    service = IngestService(repository)
    return registration, summary, registered, repository, service


def batch(summary, *, session_id=None, source_id=None, record_ids=None):
    data = json.loads((Path(__file__).parents[1] / "examples/ingest/request.json").read_text())
    data.update(device_id=str(summary.device_id), site_id=str(summary.site_id))
    data["measurement_point_id"] = str(measurement_point_id(summary.site_id))
    session_id, source_id = session_id or uuid4(), source_id or uuid4()
    data["session"].update(
        device_id=data["device_id"], site_id=data["site_id"], session_id=str(session_id)
    )
    data["source"].update(
        session_id=str(session_id),
        source_id=str(source_id),
        measurement_point_id=data["measurement_point_id"],
    )
    template = data["records"][0]
    template.update(
        device_id=data["device_id"],
        site_id=data["site_id"],
        session_id=str(session_id),
        source_id=str(source_id),
        measurement_point_id=data["measurement_point_id"],
    )
    data["records"] = [{**deepcopy(template), "record_id": str(r)} for r in record_ids or [uuid4()]]
    return UploadBatch.model_validate(data)


def body(value):
    return value.model_dump_json().encode()


def edited(value, mutate):
    data = value.model_dump(mode="json")
    mutate(data)
    return UploadBatch.model_validate(data)


def counts(repository):
    with repository.transaction() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (
                db.points,
                db.sessions,
                db.sources,
                db.snapshots,
            )
        )


def contact(repository, device_id):
    with repository.transaction() as session:
        return session.scalar(
            select(db.devices.c.last_authenticated_contact_at).where(
                db.devices.c.device_id == device_id
            )
        )


def error(operation, code):
    with pytest.raises(UploadValidationError) as failure:
        operation()
    assert failure.value.response.error.code == code
    assert str(failure.value) == code
    assert (
        failure.value.status_code
        == {
            "invalid_credential": 401,
            "device_mismatch": 403,
            "site_not_authorized": 403,
            "descriptor_conflict": 409,
            "record_conflict": 409,
            "invalid_payload": 422,
            "unsupported_schema": 422,
            "batch_too_large": 413,
            "temporarily_unavailable": 503,
        }[code]
    )
    assert failure.value.response.error.retryable == (code == "temporarily_unavailable")
    return failure.value


@pytest.mark.parametrize("token", [None, "", "unknown", "b" * 43, "é" * 43])
def test_unknown_token_no_contact(setup, token):
    _, summary, _, repository, service = setup
    error(lambda: service.ingest(token, body(batch(summary))), "invalid_credential")
    assert contact(repository, summary.device_id) is None
    assert counts(repository) == (0, 0, 0, 0)


def test_disabled_token_no_contact(setup):
    registration, summary, registered, repository, service = setup
    registration.disable_credential(summary.device_id, registered.credential_id)
    error(lambda: service.ingest(TOKEN, body(batch(summary))), "invalid_credential")
    assert contact(repository, summary.device_id) is None


@pytest.mark.parametrize(
    "kind", ["invalid", "unsupported", "oversize", "compressed", "duplicate_key"]
)
def test_rejected_payload_still_commits_contact(setup, kind):
    _, summary, _, repository, service = setup
    data = batch(summary).model_dump(mode="json")
    code, encoding = "invalid_payload", None
    if kind == "unsupported":
        data["schema_version"], code = 2, "unsupported_schema"
    elif kind == "oversize":
        data["records"] *= 201
        code = "batch_too_large"
    elif kind == "compressed":
        encoding = "gzip"
    payload = (
        b"{"
        if kind == "invalid"
        else b'{"x":1,"x":2}'
        if kind == "duplicate_key"
        else json.dumps(data).encode()
    )
    error(lambda: service.ingest(TOKEN, payload, content_encoding=encoding), code)
    assert contact(repository, summary.device_id) is not None
    assert counts(repository) == (0, 0, 0, 0)


@pytest.mark.parametrize("kind", ["device", "unknown_site", "revoked_site"])
def test_identity_and_grant_rejection_contact(setup, kind):
    registration, summary, _, repository, service = setup
    value = summary.model_copy(
        update={"device_id": uuid4()}
        if kind == "device"
        else {"site_id": uuid4()}
        if kind == "unknown_site"
        else {}
    )
    if kind == "revoked_site":
        registration.revoke_site(summary.device_id, summary.site_id)
    error(
        lambda: service.ingest(TOKEN, body(batch(value))),
        "device_mismatch" if kind == "device" else "site_not_authorized",
    )
    assert contact(repository, summary.device_id) is not None
    assert counts(repository) == (0, 0, 0, 0)


def test_contact_monotonic_and_independent_of_old_readings(setup):
    _, summary, _, repository, service = setup
    future = datetime.now(UTC) + timedelta(days=1)
    with repository.transaction() as session:
        session.execute(update(db.devices).values(last_authenticated_contact_at=future))
    service.ingest(TOKEN, body(batch(summary)))
    assert contact(repository, summary.device_id) == future
    error(lambda: service.ingest(TOKEN, b"{"), "invalid_payload")
    assert contact(repository, summary.device_id) == future


@pytest.mark.parametrize("location", ["metric", "field"])
@pytest.mark.parametrize("construction", ["copy", "construct"])
def test_constructed_float_rejected_before_serialization_without_writes(
    setup, location, construction
):
    _, summary, _, repository, service = setup
    credential = service.authenticate_contact(TOKEN)
    value = batch(summary, record_ids=[uuid4(), uuid4()])
    record = value.records[1]
    target = record.metrics if location == "metric" else record.fields[0]
    update_value = {"power_import_kw" if location == "metric" else "value": 1.23}
    target = (
        target.model_copy(update=update_value)
        if construction == "copy"
        else type(target).model_construct(**(target.model_dump(mode="python") | update_value))
    )
    record = record.model_copy(
        update={"metrics": target}
        if location == "metric"
        else {"fields": (target, *record.fields[1:])}
    )
    value = value.model_copy(update={"records": (value.records[0], record)})
    error(lambda: repository.accept(credential, value), "invalid_payload")
    assert counts(repository) == (0, 0, 0, 0)
    assert contact(repository, summary.device_id) is not None


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("schema", "unsupported_schema"),
        ("snapshot_schema", "unsupported_schema"),
        ("record_count", "batch_too_large"),
        ("snapshot_bytes", "batch_too_large"),
        ("naive_descriptor", "invalid_payload"),
    ],
)
def test_constructed_batch_keeps_safe_error_classification(setup, kind, code):
    _, summary, _, repository, service = setup
    credential = service.authenticate_contact(TOKEN)
    value = batch(summary)
    if kind == "schema":
        value = value.model_copy(update={"schema_version": 2})
    elif kind == "record_count":
        value = value.model_copy(update={"records": value.records * 201})
    elif kind == "naive_descriptor":
        descriptor = value.session.model_copy(update={"started_at": datetime(2026, 1, 1)})
        value = value.model_copy(update={"session": descriptor})
    else:
        record = value.records[0]
        if kind == "snapshot_schema":
            record = record.model_copy(update={"schema_version": 2})
        else:
            metrics = record.metrics.model_copy(update={"power_import_kw": Decimal("1" * 17000)})
            record = record.model_copy(update={"metrics": metrics})
        value = value.model_copy(update={"records": (record,)})
    error(lambda: repository.accept(credential, value), code)
    assert counts(repository) == (0, 0, 0, 0)


def test_python_decimal_revalidation_preserves_canonical_scale_nulls_and_order(setup):
    _, summary, _, repository, service = setup
    credential = service.authenticate_contact(TOKEN)
    value = batch(summary, record_ids=[uuid4(), uuid4()])
    records = tuple(
        record.model_copy(
            update={
                "metrics": record.metrics.model_copy(
                    update={"power_import_kw": Decimal("1.2300"), "voltage_l1_v": None}
                ),
                "fields": tuple(reversed(record.fields)),
            }
        )
        for record in value.records
    )
    value = value.model_copy(update={"records": records})
    expected = tuple(canonical_snapshot_bytes(record) for record in records)
    confirmation = repository.accept(credential, value)
    assert confirmation.confirmed_record_ids == tuple(record.record_id for record in records)
    assert counts(repository) == (1, 1, 1, 2)
    for record, canonical in zip(records, expected, strict=True):
        stored = repository.get_snapshot(record.device_id, record.record_id)
        assert stored.canonical == canonical
        assert stored.snapshot.metrics.power_import_kw.as_tuple() == Decimal("1.2300").as_tuple()
        assert stored.snapshot.metrics.voltage_l1_v is None
        assert stored.snapshot.fields == record.fields


def test_new_duplicate_mixed_order_roundtrip_and_lost_response_retry(setup):
    _, summary, _, repository, service = setup
    first = batch(summary, record_ids=[uuid4(), uuid4()])
    service.ingest(TOKEN, body(first))  # Simulate a committed response the Logger never receives.
    assert counts(repository) == (1, 1, 1, 2)
    confirmation = service.ingest(TOKEN, body(first))
    assert counts(repository) == (1, 1, 1, 2)
    assert (
        validate_confirmation_body(confirmation.model_dump_json().encode(), first) == confirmation
    )
    mixed = edited(
        first,
        lambda d: d["records"].insert(1, {**deepcopy(d["records"][0]), "record_id": str(uuid4())}),
    )
    confirmation = service.ingest(TOKEN, body(mixed))
    assert confirmation.confirmed_record_ids == tuple(r.record_id for r in mixed.records)
    assert counts(repository) == (1, 1, 1, 3)
    for record in mixed.records:
        stored = repository.get_snapshot(record.device_id, record.record_id)
        assert stored.canonical == canonical_snapshot_bytes(record)
        assert stored.snapshot == record


@pytest.mark.parametrize("kind", ["started_at", "source_for_session", "source_other_session"])
def test_duplicate_only_descriptors_compared(setup, kind):
    _, summary, _, repository, service = setup
    first = batch(summary)
    service.ingest(TOKEN, body(first))
    if kind == "started_at":
        changed = edited(first, lambda d: d["session"].update(started_at="2026-01-01T00:00:00Z"))
    elif kind == "source_for_session":
        source_id = str(uuid4())

        def mutate(d):
            d["source"]["source_id"] = source_id
            for record in d["records"]:
                record["source_id"] = source_id

        changed = edited(first, mutate)
    else:
        changed = batch(
            summary, source_id=first.source.source_id, record_ids=[first.records[0].record_id]
        )
    error(lambda: service.ingest(TOKEN, body(changed)), "descriptor_conflict")
    assert counts(repository) == (1, 1, 1, 1)


@pytest.mark.parametrize("kind", ["scale", "array_order"])
def test_different_hash_conflicts_after_new_record_and_rolls_back(setup, kind):
    _, summary, _, repository, service = setup
    first = batch(summary)
    service.ingest(TOKEN, body(first))

    def mutate(d):
        d["records"].insert(0, {**deepcopy(d["records"][0]), "record_id": str(uuid4())})
        if kind == "scale":
            d["records"][1]["metrics"]["power_import_kw"] = "1.0200"
        else:
            d["records"][1]["fields"].reverse()

    changed = edited(first, mutate)
    error(lambda: service.ingest(TOKEN, body(changed)), "record_conflict")
    assert counts(repository) == (1, 1, 1, 1)
    assert repository.get_snapshot(summary.device_id, changed.records[0].record_id) is None


def test_record_conflict_rolls_back_new_point_session_source_and_row(setup):
    registration, summary, _, repository, service = setup
    first = batch(summary)
    service.ingest(TOKEN, body(first))
    other = summary.model_copy(update={"site_id": uuid4()})
    registration.commit(other)
    changed = batch(other, record_ids=[uuid4(), first.records[0].record_id])
    error(lambda: service.ingest(TOKEN, body(changed)), "record_conflict")
    assert counts(repository) == (1, 1, 1, 1)


def test_failure_before_commit_rolls_back_everything_but_contact(setup):
    _, summary, _, repository, service = setup

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO snapshots"):
            raise OperationalError("secret SQL", {}, RuntimeError("secret value"))

    event.listen(repository.engine, "after_cursor_execute", fail)
    try:
        failure = error(
            lambda: service.ingest(TOKEN, body(batch(summary))), "temporarily_unavailable"
        )
        assert "secret" not in str(failure) + failure.response.model_dump_json()
    finally:
        event.remove(repository.engine, "after_cursor_execute", fail)
    assert counts(repository) == (0, 0, 0, 0)
    assert contact(repository, summary.device_id) is not None


@pytest.mark.parametrize("stage", ["contact", "accept"])
def test_connection_unavailable_fixed_503(setup, monkeypatch, stage):
    _, summary, _, repository, service = setup
    credential = service.authenticate_contact(TOKEN) if stage == "accept" else None

    def fail(*_args, **_kwargs):
        raise OperationalError("secret SQL", {}, RuntimeError("secret value"))

    monkeypatch.setattr(repository.engine, "connect", fail)
    error(
        lambda: (
            service.ingest(TOKEN, body(batch(summary)))
            if stage == "contact"
            else repository.accept(credential, batch(summary))
        ),
        "temporarily_unavailable",
    )


def test_contact_failure_prevents_acceptance(setup):
    _, summary, _, repository, service = setup

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE devices"):
            raise OperationalError("secret", {}, RuntimeError("secret"))

    event.listen(repository.engine, "after_cursor_execute", fail)
    try:
        error(lambda: service.ingest(TOKEN, body(batch(summary))), "temporarily_unavailable")
    finally:
        event.remove(repository.engine, "after_cursor_execute", fail)
    assert contact(repository, summary.device_id) is None
    assert counts(repository) == (0, 0, 0, 0)


@pytest.mark.parametrize("stage", ["contact", "accept"])
def test_pool_exhaustion_safe_503_then_retry(setup, stage):
    _, summary, _, repository, service = setup
    value = batch(summary)
    credential = service.authenticate_contact(TOKEN) if stage == "accept" else None
    with ExitStack() as stack:
        held = [stack.enter_context(repository.engine.connect()) for _ in range(10)]
        assert len({c.scalar(text("SELECT pg_backend_pid()")) for c in held}) == 10
        error(
            lambda: (
                service.ingest(TOKEN, body(value))
                if stage == "contact"
                else repository.accept(credential, value)
            ),
            "temporarily_unavailable",
        )
    assert counts(repository) == (0, 0, 0, 0)
    service.ingest(TOKEN, body(value))
    assert counts(repository) == (1, 1, 1, 1)


def race_accept(repository, values):
    barrier, state, pids = Barrier(len(values)), local(), set()

    def synchronize(connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement and not getattr(state, "started", False):
            state.started = True
            pids.add(connection.scalar(text("SELECT pg_backend_pid()")))
            barrier.wait(timeout=5)

    def accept(value):
        try:
            return repository.accept(*value)
        except UploadValidationError as exc:
            return exc.response.error.code

    event.listen(repository.engine, "before_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=len(values)) as executor:
            results = list(executor.map(accept, values))
    finally:
        event.remove(repository.engine, "before_cursor_execute", synchronize)
    assert len(pids) == len(values)
    return results


@pytest.mark.parametrize("kind", ["same", "different"])
def test_concurrent_replay_same_and_different_hash(setup, kind):
    _, summary, _, repository, service = setup
    credential = service.authenticate_contact(TOKEN)
    first = batch(summary)
    other = (
        first
        if kind == "same"
        else edited(first, lambda d: d["records"][0]["metrics"].update(power_import_kw="2.0"))
    )
    results = race_accept(repository, [(credential, first), (credential, other)])
    assert results.count("record_conflict") == (kind == "different")
    assert counts(repository) == (1, 1, 1, 1)
    stored = repository.get_snapshot(summary.device_id, first.records[0].record_id)
    assert stored.snapshot in (first.records[0], other.records[0])
    assert sum(not isinstance(r, str) for r in results) == (2 if kind == "same" else 1)


@pytest.mark.parametrize("kind", ["session", "source"])
def test_cross_device_descriptor_collision_concurrent(setup, kind):
    registration, summary, _, repository, service = setup
    other = summary.model_copy(update={"device_id": uuid4(), "site_id": uuid4(), "token": "b" * 43})
    registration.commit(other)
    first = batch(summary)
    second = batch(
        other,
        **(
            {"session_id": first.session.session_id}
            if kind == "session"
            else {"source_id": first.source.source_id}
        ),
    )
    credentials = [service.authenticate_contact(TOKEN), service.authenticate_contact(other.token)]
    results = race_accept(repository, list(zip(credentials, [first, second], strict=True)))
    assert results.count("descriptor_conflict") == 1
    assert counts(repository) == (1, 1, 1, 1)
    with repository.transaction() as session:
        descriptor, source = repository.load_descriptors(
            session, session.scalar(select(db.sessions.c.session_id))
        )
        assert (descriptor, source) in (
            (first.session, first.source),
            (second.session, second.source),
        )


def wait_blocked(engine):
    deadline = monotonic() + 3
    with engine.connect() as connection:
        while monotonic() < deadline:
            if connection.scalar(
                text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted")
            ):
                return
            sleep(0.01)
    pytest.fail("Expected an independent PostgreSQL transaction waiting on advisory lock")


@pytest.mark.parametrize("mutation", ["disable", "revoke"])
@pytest.mark.parametrize("winner", ["accept", "mutation"])
def test_disable_revoke_lock_order_both_directions(setup, mutation, winner):
    registration, summary, registered, repository, service = setup
    credential = service.authenticate_contact(TOKEN)
    value = batch(summary)
    locked, release = Event(), Event()
    pids = set()

    def hold(connection, _cursor, statement, _parameters, _context, _many):
        trigger = (
            "FROM device_site_authorizations" in statement
            if winner == "accept"
            else statement.startswith("UPDATE device_credentials")
            if mutation == "disable"
            else statement.startswith("DELETE FROM device_site_authorizations")
        )
        if trigger and not locked.is_set():
            pids.add(connection.scalar(text("SELECT pg_backend_pid()")))
            locked.set()
            assert release.wait(5)

    def mutate():
        if mutation == "disable":
            return registration.disable_credential(summary.device_id, registered.credential_id)
        return registration.revoke_site(summary.device_id, summary.site_id)

    def accept():
        try:
            return repository.accept(credential, value)
        except UploadValidationError as exc:
            return exc.response.error.code

    event.listen(repository.engine, "after_cursor_execute", hold)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(accept if winner == "accept" else mutate)
            assert locked.wait(3)
            second = executor.submit(mutate if winner == "accept" else accept)
            try:
                wait_blocked(repository.engine)
                assert not second.done()
                with repository.engine.connect() as c:
                    waiting = (
                        c.execute(
                            text(
                                "SELECT pid FROM pg_locks WHERE locktype='advisory' AND NOT granted"
                            )
                        )
                        .scalars()
                        .all()
                    )
                assert waiting and all(pid not in pids for pid in waiting)
            finally:
                release.set()
            first_result, second_result = first.result(timeout=5), second.result(timeout=5)
    finally:
        release.set()
        event.remove(repository.engine, "after_cursor_execute", hold)
    accepted = first_result if winner == "accept" else second_result
    if winner == "accept":
        assert accepted.confirmed_record_ids == (value.records[0].record_id,)
        assert counts(repository) == (1, 1, 1, 1)
    else:
        assert accepted == (
            "invalid_credential" if mutation == "disable" else "site_not_authorized"
        )
        assert counts(repository) == (0, 0, 0, 0)
    # Later accepts must see the change, even when an earlier batch already committed.
    error(
        lambda: repository.accept(credential, value),
        "invalid_credential" if mutation == "disable" else "site_not_authorized",
    )
    assert contact(repository, summary.device_id) is not None


def test_real_lock_timeout_safe_retryable(setup):
    _, summary, _, repository, service = setup
    credential = service.authenticate_contact(TOKEN)
    value = batch(summary)

    def shorten(connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            connection.exec_driver_sql("SET LOCAL lock_timeout = '50ms'")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with repository.transaction() as holder:
            acquire_mutation_locks(holder, device=[summary.device_id])
            event.listen(repository.engine, "before_cursor_execute", shorten)
            try:
                future = executor.submit(repository.accept, credential, value)
                error(lambda: future.result(timeout=3), "temporarily_unavailable")
            finally:
                event.remove(repository.engine, "before_cursor_execute", shorten)
    assert counts(repository) == (0, 0, 0, 0)
    service.ingest(TOKEN, body(value))
    assert counts(repository) == (1, 1, 1, 1)


@pytest.mark.parametrize("winner", ["contact", "disable"])
def test_contact_disable_race_rechecks_enabled_under_device_lock(setup, winner):
    registration, summary, registered, repository, service = setup
    locked, release = Event(), Event()

    def hold(_connection, _cursor, statement, _parameters, _context, _many):
        trigger = (
            statement.startswith("UPDATE devices")
            if winner == "contact"
            else statement.startswith("UPDATE device_credentials")
        )
        if trigger:
            locked.set()
            assert release.wait(5)

    def authenticate():
        try:
            return service.authenticate_contact(TOKEN)
        except UploadValidationError as exc:
            return exc.response.error.code

    def disable():
        return registration.disable_credential(summary.device_id, registered.credential_id)

    event.listen(repository.engine, "after_cursor_execute", hold)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(authenticate if winner == "contact" else disable)
            assert locked.wait(3)
            second = executor.submit(disable if winner == "contact" else authenticate)
            try:
                wait_blocked(repository.engine)
                assert not second.done()
            finally:
                release.set()
            first_result, second_result = first.result(timeout=5), second.result(timeout=5)
    finally:
        release.set()
        event.remove(repository.engine, "after_cursor_execute", hold)
    authenticated = first_result if winner == "contact" else second_result
    if winner == "contact":
        assert authenticated.device_id == summary.device_id
        assert contact(repository, summary.device_id) is not None
    else:
        assert authenticated == "invalid_credential"
        assert contact(repository, summary.device_id) is None
    previous_contact = contact(repository, summary.device_id)
    error(lambda: service.authenticate_contact(TOKEN), "invalid_credential")
    assert contact(repository, summary.device_id) == previous_contact
    assert counts(repository) == (0, 0, 0, 0)
