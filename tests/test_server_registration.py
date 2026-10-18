"""Registration contract and real PostgreSQL concurrency/access preservation."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid1, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select, text

from watt_profile.contracts.domain import (
    SessionDescriptor,
    Snapshot,
    SourceDescriptor,
    measurement_point_id,
)
from watt_profile.server.registration import (
    MAX_REGISTRATION_BYTES,
    DeviceInput,
    InvalidRegistration,
    ObjectNotFound,
    PageOptions,
    RegistrationConflict,
    RegistrationService,
    RegistrationSummary,
    SiteInput,
    SitePatch,
    SiteSearch,
    parse_registration_summary,
)
from watt_profile.server.storage import schema as db
from watt_profile.server.storage.registration import RegistrationRepository, StorageUnavailable
from watt_profile.server.storage.repository import acquire_mutation_locks

TOKEN = "a" * 43
NOW = datetime(2025, 1, 1, tzinfo=UTC)


def summary(**changes):
    return RegistrationSummary(
        site_id=uuid4(),
        device_id=uuid4(),
        address="Synthetic\n楼层",
        description="First\nSecond",
        token=TOKEN,
        **changes,
    )


@pytest.fixture
def repo(server_engine):
    return RegistrationRepository(server_engine)


def counts(repo):
    with repo.transaction() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(table))
            for table in (db.sites, db.devices, db.credentials, db.authorizations)
        )


def changed(value, **updates):
    return RegistrationSummary.model_validate({**value.model_dump(), **updates})


def test_strict_multiline_summary_and_no_repr_secret():
    value = summary()
    encoded = value.model_dump_json(indent=2)
    assert parse_registration_summary(encoded) == value
    assert value.token not in repr(value)
    assert len(value.address) < 500


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: {**d, "extra": 1},
        lambda d: {k: v for k, v in d.items() if k != "description"},
        lambda d: {**d, "device_id": str(uuid1())},
        lambda d: {**d, "site_id": "00000000-0000-4000-7000-000000000001"},
        lambda d: {**d, "token": "é" * 43},
        lambda d: {**d, "token": "a" * 42},
        lambda d: {**d, "token": "a" * 44},
        lambda d: {**d, "token": "a" * 42 + "/"},
        lambda d: {**d, "token": 1},
        lambda d: {**d, "address": "a" * 501},
        lambda d: {**d, "description": "a" * 2001},
        lambda d: {**d, "address": None},
    ],
)
def test_invalid_fields_are_safe(mutation):
    with pytest.raises(InvalidRegistration) as failure:
        parse_registration_summary(json.dumps(mutation(summary().model_dump(mode="json"))))
    assert str(failure.value) == "invalid_registration"
    assert failure.value.__cause__ is None


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"[]",
        b"null",
        b"\xff",
        b" " * (MAX_REGISTRATION_BYTES + 1),
        '{"token":"first","token":"second"}',
        '{"x":{"a":1,"a":2}}',
    ],
)
def test_malformed_duplicate_oversized_body(body):
    with pytest.raises(InvalidRegistration):
        parse_registration_summary(body)


def test_summary_bounds_and_urlsafe_token():
    value = changed(summary(), address="é" * 500, description="\n" * 2000, token="_" * 42 + "-")
    assert parse_registration_summary(value.model_dump_json()) == value
    assert parse_registration_summary(changed(value, address="", description="").model_dump_json())


def test_preview_no_writes_or_secrets_and_commit_hash_only(repo, caplog):
    value = summary()
    service = RegistrationService(repo)
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(repo.engine, "before_cursor_execute", observe)
    try:
        preview = service.preview(value)
    finally:
        event.remove(repo.engine, "before_cursor_execute", observe)
    assert counts(repo) == (0, 0, 0, 0)
    assert all(
        not sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
    )
    assert preview.actions.model_dump() == dict.fromkeys(
        ("site", "device", "credential", "grant"), "create"
    )
    assert preview.credential_id is None
    assert TOKEN not in preview.model_dump_json() + repr(preview) + caplog.text
    applied = service.commit(value)
    assert applied.credential_id is not None
    assert applied.site.site_id == value.site_id and applied.device_id == value.device_id
    with repo.transaction() as session:
        row = session.execute(select(db.credentials)).mappings().one()
        assert row["token_sha256"] == sha256(TOKEN.encode()).hexdigest()
        assert TOKEN not in str(dict(row))
    assert (
        TOKEN not in applied.model_dump_json() + repo.get_device(value.device_id).model_dump_json()
    )
    assert "token_sha256" not in repo.get_device(value.device_id).model_dump_json()


def test_idempotent_known_uuid_metadata_and_a_b_grants(repo):
    value = summary()
    first = repo.commit(value)
    again = repo.commit(changed(value, address="different", description="different"))
    assert again.credential_id == first.credential_id and again.site == first.site
    assert again.metadata_differs
    assert set(again.actions.model_dump().values()) == {"reuse"}
    other = changed(value, site_id=uuid4())
    registered = repo.commit(other)
    assert registered.actions.credential == "reuse" and registered.actions.grant == "create"
    assert registered.credential_id == first.credential_id
    assert {site.site_id for site in repo.get_device(value.device_id).sites} == {
        value.site_id,
        other.site_id,
    }
    assert counts(repo) == (2, 1, 1, 2)
    repo.revoke_site(value.device_id, other.site_id)
    repo.revoke_site(value.device_id, other.site_id)
    assert [site.site_id for site in repo.get_device(value.device_id).sites] == [value.site_id]
    repo.grant_site(value.device_id, other.site_id)
    repo.grant_site(value.device_id, other.site_id)
    assert counts(repo) == (2, 1, 1, 2)


def test_preview_is_rechecked_and_site_edits_are_explicit(repo):
    value = summary()
    assert repo.preview(value).actions.site == "create"
    persisted = repo.create_site(
        SiteInput(site_id=value.site_id, address="server", description="server")
    )
    result = repo.commit(value)
    assert result.site == persisted and result.actions.site == "reuse" and result.metadata_differs
    assert repo.create_site(SiteInput(site_id=value.site_id, address="ignored")) == persisted
    assert (
        repo.edit_site(value.site_id, SitePatch(address="edited\naddress")).description == "server"
    )
    assert repo.get_site(value.site_id).address == "edited\naddress"
    for patch in (SitePatch(), SitePatch(address=None)):
        with pytest.raises(InvalidRegistration):
            repo.edit_site(value.site_id, patch)


@pytest.mark.parametrize("kind", ["new_token", "cross_device", "disabled"])
def test_conflicts_no_partial_site_device_creation(repo, kind):
    value = summary()
    initial = repo.commit(value)
    if kind == "disabled":
        state = repo.disable_credential(value.device_id, initial.credential_id)
        assert repo.disable_credential(value.device_id, initial.credential_id) == state
    invalid = changed(
        value,
        site_id=uuid4(),
        **(
            {"token": "b" * 43}
            if kind == "new_token"
            else {"device_id": uuid4()}
            if kind == "cross_device"
            else {}
        ),
    )
    for operation in (repo.preview, repo.commit):
        with pytest.raises(RegistrationConflict) as failure:
            operation(invalid)
        assert str(failure.value) == "registration_conflict"
        assert counts(repo) == (1, 1, 1, 1)


def test_standalone_device_first_credential_disable_and_grants(repo):
    device_id = uuid4()
    device = repo.create_device(DeviceInput(device_id=device_id))
    assert repo.create_device(DeviceInput(device_id=device_id)) == device
    credential = repo.register_credential(device_id, TOKEN)
    assert repo.register_credential(device_id, TOKEN) == credential
    site = repo.create_site(SiteInput(site_id=uuid4()))
    assert (
        repo.commit(changed(summary(), device_id=device_id, site_id=site.site_id)).credential_id
        == credential.credential_id
    )
    with pytest.raises(RegistrationConflict):
        repo.register_credential(device_id, "b" * 43)
    repo.disable_credential(device_id, credential.credential_id)
    with pytest.raises(RegistrationConflict):
        repo.register_credential(device_id, TOKEN)
    assert counts(repo) == (1, 1, 1, 1)


def test_existing_device_without_credential_registration(repo):
    value = summary()
    repo.create_device(DeviceInput(device_id=value.device_id))
    result = repo.commit(value)
    assert result.actions.device == "reuse" and result.actions.credential == "create"


def test_absent_objects_do_not_create_grants_or_credentials(repo):
    value = summary()
    repo.commit(value)
    operations = [
        lambda: repo.get_site(uuid4()),
        lambda: repo.get_device(uuid4()),
        lambda: repo.edit_site(uuid4(), SitePatch(address="x")),
        lambda: repo.register_credential(uuid4(), "b" * 43),
        lambda: repo.disable_credential(uuid4(), uuid4()),
        lambda: repo.disable_credential(value.device_id, uuid4()),
        lambda: repo.grant_site(uuid4(), value.site_id),
        lambda: repo.grant_site(value.device_id, uuid4()),
        lambda: repo.revoke_site(uuid4(), value.site_id),
        lambda: repo.revoke_site(value.device_id, uuid4()),
    ]
    for operation in operations:
        with pytest.raises(ObjectNotFound):
            operation()
    assert counts(repo) == (1, 1, 1, 1)


def test_literal_case_insensitive_search_pagination_uuid_order(repo):
    sites = [
        SiteInput(site_id=uuid4(), address=address)
        for address in ("Synthetic%_Station", "synthetic station", "other\nSTATION")
    ]
    for site in sites:
        repo.create_site(site)
    ordered = sorted(sites, key=lambda site: site.site_id)
    assert repo.site_page().items == tuple(ordered)
    page = repo.site_page(SiteSearch(offset=1, limit=1))
    assert page.items == (ordered[1],) and page.total == 3
    assert repo.site_page(SiteSearch(q="%_")).items == (sites[0],)
    assert repo.site_page(SiteSearch(q="station")).total == 3
    assert repo.site_page(SiteSearch(site_id=sites[1].site_id)).items == (sites[1],)
    assert repo.site_page(SiteSearch(q="missing")).total == 0
    assert repo.site_page(SiteSearch(offset=99)).items == ()
    with pytest.raises(InvalidRegistration):
        repo.site_page(SiteSearch(q="x", site_id=sites[0].site_id))
    devices = [repo.create_device(DeviceInput(device_id=uuid4())) for _ in range(3)]
    assert repo.device_page(PageOptions(offset=1, limit=1)).items == (
        sorted(devices, key=lambda d: d.device_id)[1],
    )
    assert repo.device_page().total == 3
    for options in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": True}):
        with pytest.raises(ValidationError):
            PageOptions(**options)


def test_registration_atomic_rollback_at_last_write(repo):
    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO device_site_authorizations"):
            raise RuntimeError("synthetic_failure_after_all_inserts")

    event.listen(repo.engine, "after_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic_failure"):
            repo.commit(summary())
    finally:
        event.remove(repo.engine, "after_cursor_execute", fail)
    assert counts(repo) == (0, 0, 0, 0)


def test_historical_snapshots_descriptors_retained_after_edit_revoke_disable(repo):
    value = summary()
    result = repo.commit(value)
    descriptor = SessionDescriptor(
        session_id=uuid4(), device_id=value.device_id, site_id=value.site_id, started_at=NOW
    )
    source = SourceDescriptor(
        source_id=uuid4(),
        session_id=descriptor.session_id,
        measurement_point_id=measurement_point_id(value.site_id),
    )
    data = json.loads((Path(__file__).parent.parent / "examples/ingest/request.json").read_text())[
        "records"
    ][0]
    data.update(
        record_id=str(uuid4()),
        device_id=str(value.device_id),
        site_id=str(value.site_id),
        session_id=str(descriptor.session_id),
        source_id=str(source.source_id),
        measurement_point_id=str(source.measurement_point_id),
    )
    snapshot = Snapshot.model_validate(data)
    with repo.transaction() as session:
        repo.insert_descriptors(session, descriptor, source)
        repo.insert_snapshot(session, snapshot, NOW)
    before = repo.get_snapshot(snapshot.device_id, snapshot.record_id)
    repo.edit_site(value.site_id, SitePatch(address="edited"))
    repo.revoke_site(value.device_id, value.site_id)
    repo.disable_credential(value.device_id, result.credential_id)
    assert repo.get_snapshot(snapshot.device_id, snapshot.record_id) == before
    with repo.transaction() as session:
        assert repo.load_descriptors(session, descriptor.session_id) == (descriptor, source)


def concurrent(repo, values):
    barrier = Barrier(len(values))
    pids = set()

    def operation(value):
        # Separate simultaneous connections establish the actual database test context.
        with repo.engine.connect() as connection:
            pids.add(connection.scalar(text("SELECT pg_backend_pid()")))
            barrier.wait(timeout=5)
        try:
            return repo.commit(value)
        except RegistrationConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=len(values)) as executor:
        results = list(executor.map(operation, values))
    assert len(pids) == len(values)
    return results


@pytest.mark.parametrize("kind", ["same", "a_b", "new_token", "same_site"])
def test_concurrent_registration_shared_objects(repo, kind):
    value = summary()
    other = (
        value
        if kind == "same"
        else changed(value, site_id=uuid4())
        if kind == "a_b"
        else changed(value, site_id=uuid4(), token="b" * 43)
        if kind == "new_token"
        else changed(value, device_id=uuid4(), token="b" * 43)
    )
    results = concurrent(repo, [value, other])
    assert results.count("conflict") == (1 if kind == "new_token" else 0)
    assert (
        counts(repo)
        == {
            "same": (1, 1, 1, 1),
            "a_b": (2, 1, 1, 2),
            "new_token": (1, 1, 1, 1),
            "same_site": (1, 2, 2, 2),
        }[kind]
    )


def test_concurrent_cross_device_hash_unique_conflict_rolls_back(repo):
    # Different device/site locks allow both to observe an absent hash before either inserts.
    barrier = Barrier(2)

    def race(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO device_credentials"):
            barrier.wait(timeout=5)

    event.listen(repo.engine, "before_cursor_execute", race)
    try:
        results = concurrent(repo, [summary(), summary()])
    finally:
        event.remove(repo.engine, "before_cursor_execute", race)
    assert results.count("conflict") == 1
    assert counts(repo) == (1, 1, 1, 1)


def assert_waiting_advisory_lock(holder):
    # before_cursor_execute fires before PostgreSQL receives the request.
    deadline = monotonic() + 3
    while monotonic() < deadline:
        if (
            holder.scalar(
                text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted")
            )
            == 1
        ):
            return
        sleep(0.01)
    pytest.fail("Expected an independent PostgreSQL transaction waiting on advisory lock")


def test_registration_waits_for_cross_connection_shared_device_lock(repo):
    value = summary()
    attempted = Event()

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            attempted.set()

    event.listen(repo.engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with repo.transaction() as holder:
                acquire_mutation_locks(holder, device=[value.device_id])
                attempted.clear()
                pending = executor.submit(repo.commit, value)
                assert attempted.wait(2) and not pending.done()
                assert_waiting_advisory_lock(holder)
            assert pending.result(timeout=5).credential_id
    finally:
        event.remove(repo.engine, "before_cursor_execute", observe)


def test_invalid_constructed_summary_revalidated_before_writes(repo):
    with pytest.raises(InvalidRegistration):
        repo.commit(summary().model_copy(update={"token": "invalid"}))
    assert counts(repo) == (0, 0, 0, 0)


def test_storage_connection_failure_safe_retryable(repo, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def fail(*_args, **_kwargs):
        raise OperationalError("synthetic SQL secret", {}, RuntimeError("synthetic secret"))

    monkeypatch.setattr(repo.engine, "connect", fail)
    with pytest.raises(StorageUnavailable) as failure:
        repo.commit(summary())
    assert str(failure.value) == "storage_unavailable"
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize("operation", ["preview", "commit"])
def test_real_pool_exhaustion_is_safe_and_release_allows_retry(repo, operation):
    value = summary()
    with ExitStack() as connections:
        held = [connections.enter_context(repo.engine.connect()) for _ in range(10)]
        assert (
            len({connection.scalar(text("SELECT pg_backend_pid()")) for connection in held}) == 10
        )
        assert repo.engine.pool.checkedout() == 10
        with pytest.raises(StorageUnavailable) as failure:
            getattr(repo, operation)(value)
        assert str(failure.value) == "storage_unavailable"
        assert failure.value.__cause__ is None
        assert failure.value.__suppress_context__
    assert repo.engine.pool.checkedout() == 0
    assert counts(repo) == (0, 0, 0, 0)
    result = getattr(repo, operation)(value)
    assert result.actions.model_dump() == dict.fromkeys(
        ("site", "device", "credential", "grant"), "create"
    )
    assert counts(repo) == ((0, 0, 0, 0) if operation == "preview" else (1, 1, 1, 1))


def test_commit_rechecks_disabled_credential_after_preview(repo):
    value = summary()
    initial = repo.commit(value)
    additional = changed(value, site_id=uuid4())
    assert repo.preview(additional).actions.grant == "create"
    repo.disable_credential(value.device_id, initial.credential_id)
    with pytest.raises(RegistrationConflict):
        repo.commit(additional)
    assert counts(repo) == (1, 1, 1, 1)


@pytest.mark.parametrize("token", ["short", "é" * 43, 5])
def test_standalone_credential_token_validation_is_safe(repo, token):
    device_id = uuid4()
    repo.create_device(DeviceInput(device_id=device_id))
    with pytest.raises(InvalidRegistration) as failure:
        repo.register_credential(device_id, token)
    assert str(failure.value) == "invalid_registration"
    assert counts(repo) == (0, 1, 0, 0)


@pytest.mark.parametrize("kind", ["disable", "grant", "revoke", "site_edit"])
def test_access_mutations_use_shared_cross_connection_locks(repo, kind):
    value = summary()
    registered = repo.commit(value)
    operations = {
        "disable": lambda: repo.disable_credential(value.device_id, registered.credential_id),
        "grant": lambda: repo.grant_site(value.device_id, value.site_id),
        "revoke": lambda: repo.revoke_site(value.device_id, value.site_id),
        "site_edit": lambda: repo.edit_site(value.site_id, SitePatch(address="explicit")),
    }
    attempted = Event()

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            attempted.set()

    event.listen(repo.engine, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with repo.transaction() as holder:
                acquire_mutation_locks(
                    holder,
                    **(
                        {"site": [value.site_id]}
                        if kind == "site_edit"
                        else {"device": [value.device_id]}
                    ),
                )
                attempted.clear()
                pending = executor.submit(operations[kind])
                assert attempted.wait(2) and not pending.done()
                assert_waiting_advisory_lock(holder)
            pending.result(timeout=5)
    finally:
        event.remove(repo.engine, "before_cursor_execute", observe)


def test_real_lock_timeout_is_safe_retryable_and_rolls_back(repo):
    value = summary()

    def shorten(connection, _cursor, statement, _parameters, _context, _many):
        if "pg_advisory_xact_lock" in statement:
            connection.exec_driver_sql("SET LOCAL lock_timeout = '50ms'")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with repo.transaction() as holder:
            acquire_mutation_locks(holder, device=[value.device_id])
            event.listen(repo.engine, "before_cursor_execute", shorten)
            try:
                pending = executor.submit(repo.commit, value)
                with pytest.raises(StorageUnavailable, match="^storage_unavailable$"):
                    pending.result(timeout=3)
            finally:
                event.remove(repo.engine, "before_cursor_execute", shorten)
    assert counts(repo) == (0, 0, 0, 0)


def test_display_name_fallbacks_and_clear(repo):
    from watt_profile.server.registration import DevicePatch

    site = repo.create_site(
        SiteInput(site_id=uuid4(), address="Synthetic address", display_name="Home")
    )
    assert site.display_label == "Home"
    site = repo.edit_site(site.site_id, SitePatch(display_name=""))
    assert site.display_label == "Synthetic address"
    site = repo.edit_site(site.site_id, SitePatch(address=""))
    assert site.display_label == str(site.site_id)[:8]
    device = repo.create_device(DeviceInput(device_id=uuid4()))
    assert device.display_label == str(device.device_id)[:8]
    assert (
        repo.edit_device(device.device_id, DevicePatch(display_name="Logger")).display_label
        == "Logger"
    )
    assert (
        repo.edit_device(device.device_id, DevicePatch(display_name="")).display_label
        == str(device.device_id)[:8]
    )
    with pytest.raises(InvalidRegistration):
        repo.edit_site(site.site_id, SitePatch(display_name=None))
