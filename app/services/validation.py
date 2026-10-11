"""Per-recipient business validation, applied during job processing.

This is tier 2 of the validation model (tier 1 is Pydantic at the API
boundary). Rules here fail only the individual recipient, never the job:

* names must be at least 2 characters after stripping;
* names must not contain control characters;
* text printed on the certificate must be encodable in WinAnsi (cp1252) —
  the encoding of the PDF base-14 fonts. ReportLab does *not* raise on
  unencodable characters, it silently draws garbage, so we check explicitly;
* duplicate (name, email) rows within the same job are rejected — the first
  occurrence wins, later ones are marked failed.

Known limitation / improvement path: registering a Unicode TTF with ReportLab
would lift the cp1252 restriction (see README).
"""
from __future__ import annotations

import unicodedata

# PDF base-14 fonts use WinAnsiEncoding, which is essentially cp1252.
FONT_ENCODING = "cp1252"


class RecipientValidationError(ValueError):
    """Raised for a problem with ONE recipient; message is safe to expose."""


def recipient_key(full_name: str, email: str | None) -> tuple[str, str]:
    """Normalised identity used for duplicate detection within a job."""
    return (full_name.strip().casefold(), (email or "").casefold())


def validate_recipient(
    full_name: str,
    email: str | None,
    seen: dict[tuple[str, str], int],
) -> None:
    """Check one recipient against the per-recipient rules and the duplicate map.

    This function does NOT mutate ``seen``. The caller registers the recipient
    with ``seen[recipient_key(...)] = position`` only after the certificate has
    been rendered successfully, so a row that fails later in the pipeline cannot
    block a valid duplicate that follows it.
    """
    name = full_name.strip()

    if len(name) < 2:
        raise RecipientValidationError("full_name must be at least 2 characters")

    for ch in name:
        if unicodedata.category(ch) == "Cc":
            raise RecipientValidationError("full_name must not contain control characters")

    _ensure_renderable(name, "full_name")

    key = recipient_key(name, email)
    if key in seen:
        raise RecipientValidationError(
            f"duplicate recipient in this job (same name and email as position {seen[key]})"
        )


def _ensure_renderable(text: str, field: str) -> None:
    try:
        text.encode(FONT_ENCODING)
    except UnicodeEncodeError as exc:
        bad = text[exc.start]
        raise RecipientValidationError(
            f"{field} contains character {bad!r} (U+{ord(bad):04X}) which the "
            "certificate template font does not support"
        ) from None
