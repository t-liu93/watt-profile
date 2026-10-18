"""Server-only display names; identities and Logger summaries stay separate."""

import unicodedata
from typing import Annotated

from pydantic import AfterValidator, Field, StrictStr


def validate_display_name(value: str) -> str:
    if any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in value):
        raise ValueError("invalid_display_name")
    return value


DisplayName = Annotated[StrictStr, Field(max_length=100), AfterValidator(validate_display_name)]
