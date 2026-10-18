"""Strict, secret-safe registration values and HTTP-independent service facade."""

import json
import re
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import ConfigDict, Field, StrictInt, StrictStr, ValidationError, field_validator

from watt_profile.contracts.domain import UUID4, DomainModel, UtcTime
from watt_profile.server.names import DisplayName

if TYPE_CHECKING:
    from watt_profile.server.storage.registration import RegistrationRepository

MAX_REGISTRATION_BYTES = 32 * 1024
Address = Annotated[StrictStr, Field(max_length=500)]
Description = Annotated[StrictStr, Field(max_length=2000)]
Action = Literal["create", "reuse"]


class InvalidRegistration(ValueError):
    def __init__(self):
        super().__init__("invalid_registration")


class RegistrationConflict(ValueError):
    def __init__(self):
        super().__init__("registration_conflict")


class ObjectNotFound(ValueError):
    def __init__(self):
        super().__init__("object_not_found")


class RegistrationSummary(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    site_id: UUID4
    address: Address
    description: Description
    device_id: UUID4
    token: StrictStr = Field(repr=False)

    @field_validator("token")
    @classmethod
    def check_token(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", value, flags=re.ASCII) is None:
            raise ValueError("invalid_token")
        return value


class CredentialInput(DomainModel):
    token: StrictStr = Field(repr=False)

    @field_validator("token")
    @classmethod
    def check_token(cls, value: str) -> str:
        return RegistrationSummary.check_token(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRegistration()
        result[key] = value
    return result


def parse_registration_summary(body: bytes | str) -> RegistrationSummary:
    """Bound bytes before decoding; duplicate keys and parser details never escape."""
    try:
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, bytes) or len(body) > MAX_REGISTRATION_BYTES:
            raise InvalidRegistration()
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        return RegistrationSummary.model_validate(data)
    except (ValueError, TypeError, RecursionError):
        raise InvalidRegistration() from None


def validate_summary(summary: RegistrationSummary) -> RegistrationSummary:
    try:
        return RegistrationSummary.model_validate(summary.model_dump())
    except (ValidationError, ValueError, TypeError):
        raise InvalidRegistration() from None


class SiteInput(DomainModel):
    display_name: DisplayName = ""
    site_id: UUID4
    address: Address = ""
    description: Description = ""

    @property
    def display_label(self) -> str:
        return self.display_name or self.address or str(self.site_id)[:8]


class SitePatch(DomainModel):
    display_name: DisplayName | None = None
    address: Address | None = None
    description: Description | None = None

    def changes(self) -> dict[str, str]:
        values = self.model_dump(exclude_unset=True)
        if not values or any(value is None for value in values.values()):
            raise InvalidRegistration()
        return values


class DeviceInput(DomainModel):
    device_id: UUID4


class DevicePatch(DomainModel):
    display_name: DisplayName


class Device(DomainModel):
    display_name: DisplayName = ""
    device_id: UUID4
    created_at: UtcTime
    last_authenticated_contact_at: UtcTime | None

    @property
    def display_label(self) -> str:
        return self.display_name or str(self.device_id)[:8]


class Credential(DomainModel):
    credential_id: UUID4
    device_id: UUID4
    created_at: UtcTime
    disabled_at: UtcTime | None


class Grant(DomainModel):
    device_id: UUID4
    site_id: UUID4


class PageOptions(DomainModel):
    offset: Annotated[StrictInt, Field(ge=0)] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=100)] = 50


class SiteSearch(PageOptions):
    site_id: UUID4 | None = None
    q: StrictStr | None = None

    def predicate(self):
        if self.site_id is not None and self.q is not None:
            raise InvalidRegistration()


class Page[T](DomainModel):
    items: tuple[T, ...]
    total: int


class RegistrationActions(DomainModel):
    site: Action
    device: Action
    credential: Action
    grant: Action


class RegistrationResult(DomainModel):
    site: SiteInput
    device_id: UUID4
    credential_id: UUID4 | None
    actions: RegistrationActions
    metadata_differs: bool
    credential_mask: Literal["********"] = "********"


class DeviceDetail(DomainModel):
    device: Device
    credentials: tuple[Credential, ...]
    sites: tuple[SiteInput, ...]


class RegistrationService:
    """Registration processing has no request, application or Logger dependencies."""

    def __init__(self, repository: "RegistrationRepository"):
        self.repository = repository

    def preview(self, summary: RegistrationSummary) -> RegistrationResult:
        return self.repository.preview(validate_summary(summary))

    def commit(self, summary: RegistrationSummary) -> RegistrationResult:
        return self.repository.commit(validate_summary(summary))
