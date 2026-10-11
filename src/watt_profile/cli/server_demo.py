"""Explicit, development-only synthetic data preparation and HTTP replay."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import os
import secrets
import stat
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from watt_profile.contracts.domain import (
    Metrics,
    Quality,
    SanitizedField,
    SessionDescriptor,
    Snapshot,
    SourceDescriptor,
    measurement_point_id,
)
from watt_profile.contracts.upload import UploadBatch, validate_confirmation_body
from watt_profile.server.registration import RegistrationSummary
from watt_profile.server.settings import _valid_public_origin


class DemoError(ValueError):
    """Safe fixed diagnostic, without user input or credentials."""


def prepare(directory: Path) -> None:
    directory.mkdir(mode=0o700)  # No existing directory, including symlinks.
    now, device, token = datetime.now(UTC), uuid4(), secrets.token_urlsafe(32)
    sites = {label: uuid4() for label in ("A", "B")}

    def write(name, value):
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")

    write("token", token)
    for label, site in sites.items():
        summary = RegistrationSummary(
            site_id=site,
            device_id=device,
            address=f"Synthetic site {label}",
            description="Disposable development demonstration\nNo real meter data",
            token=token,
        )
        write(f"registration-{label}.json", summary.model_dump_json(indent=2))
    for index, label in enumerate(("A", "B", "A")):
        start = now + timedelta(hours=index - 3)
        site, session_id, source_id = sites[label], uuid4(), uuid4()
        point = measurement_point_id(site)
        session = SessionDescriptor(
            session_id=session_id, device_id=device, site_id=site, started_at=start
        )
        source = SourceDescriptor(
            source_id=source_id, session_id=session_id, measurement_point_id=point
        )
        records = []
        for offset in (0, 1):
            metrics = dict.fromkeys(Metrics.model_fields)
            metrics.update(power_import_kw=Decimal("1.020"), power_export_kw=Decimal("0.000"))
            records.append(
                Snapshot(
                    record_id=uuid4(),
                    device_id=device,
                    site_id=site,
                    session_id=session_id,
                    source_id=source_id,
                    measurement_point_id=point,
                    meter_time_raw=None,
                    meter_time_utc=None,
                    received_at=start + timedelta(seconds=offset),
                    sampled_at=start + timedelta(seconds=offset, microseconds=250),
                    metrics=Metrics(**metrics),
                    fields=(
                        SanitizedField(
                            code="1-0:1.7.0",
                            raw_values=("1.020*kW",),
                            value=Decimal("1.020"),
                            unit="kW",
                            redacted=False,
                        ),
                        SanitizedField(
                            code="0-0:96.1.1",
                            raw_values=("<redacted>",),
                            value=None,
                            unit=None,
                            redacted=True,
                        ),
                    ),
                    quality=Quality(frame_length=120, issues=()),
                )
            )
        payload = UploadBatch(
            schema_version=1,
            device_id=device,
            site_id=site,
            measurement_point_id=point,
            session=session,
            source=source,
            records=tuple(records),
        )
        write(f"batch-{index + 1}-{label}.json", payload.model_dump_json(indent=2))


def _read(directory: Path, name: str) -> str:
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
        raise DemoError("demo directory must be private")
    descriptor = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_size > 100_000
        ):
            raise DemoError("demo file must be private and bounded")
        return stream.read()


def _origin(origin: str, allow_development_network: bool):
    if not _valid_public_origin(origin):
        raise DemoError("invalid development origin")
    parsed = urlsplit(origin)
    host = parsed.hostname
    allowed = host == "localhost"  # Exact spelling; no suffix DNS aliases.
    try:
        allowed |= ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if (
        allow_development_network
        and host == "server"
        and parsed.scheme == "http"
        and parsed.port == 8080
    ):
        allowed = True
    if not allowed:
        raise DemoError("origin must be loopback or explicit disposable server service")
    return parsed


def ingest(
    directory: Path,
    origin: str,
    *,
    site: str | None = None,
    allow_development_network: bool = False,
) -> int:
    parsed = _origin(origin, allow_development_network)
    token = _read(directory, "token").removesuffix("\n")
    if len(token) != 43 or any(
        c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in token
    ):
        raise DemoError("invalid demo credential")
    # Read and validate all selected inputs before any network request.
    batches = [
        UploadBatch.model_validate_json(_read(directory, f"batch-{index}-{label}.json"))
        for index, label in ((1, "A"), (2, "B"), (3, "A"))
        if site is None or site == label
    ]
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
    if parsed.hostname == "server":
        public = _origin(os.environ.get("WATT_SERVER_PUBLIC_ORIGIN", ""), False)
        headers["Host"] = public.netloc
    count = 0
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    for batch in batches:
        connection = connection_type(parsed.hostname, parsed.port, timeout=15)
        try:
            connection.request(
                "POST", "/api/v1/ingest/batches", batch.model_dump_json().encode(), headers
            )
            response = connection.getresponse()
            body = response.read(100_001)
            if response.status != 200:
                # No redirects, retry or echo of potentially hostile response content.
                raise DemoError(f"ingest rejected (HTTP {response.status})")
            confirmation = validate_confirmation_body(body, batch)
            if confirmation.confirmed_record_ids != tuple(r.record_id for r in batch.records):
                raise DemoError("invalid confirmation order")
            count += len(batch.records)
        finally:
            connection.close()
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Disposable development demo only.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("prepare")
    create.add_argument("--output", required=True, type=Path)
    replay = commands.add_parser("ingest")
    replay.add_argument("--directory", required=True, type=Path)
    replay.add_argument("--server-origin", required=True)
    replay.add_argument("--site", choices=("A", "B"))
    replay.add_argument("--allow-development-network", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.output)
            print("Private synthetic demo prepared.")
        else:
            count = ingest(
                args.directory,
                args.server_origin,
                site=args.site,
                allow_development_network=args.allow_development_network,
            )
            print(f"Confirmed {count} synthetic records (including replayed records).")
    except DemoError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, http.client.HTTPException):
        print("Unable to process development demo.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
