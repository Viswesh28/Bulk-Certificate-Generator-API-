"""Pydantic schemas (API request/response bodies).

Validation is deliberately two-tiered:

1. **Structural validation happens here** (types, lengths, email format,
   control characters, job-wide fields such as ``course_title``). A request
   that is structurally broken is rejected *as a whole* with HTTP 422 —
   it indicates a buggy client, not bad individual rows.

2. **Per-recipient business validation happens during processing**
   (``app.services.validation``): duplicates within a batch, characters the
   certificate font cannot render, etc. These fail only the offending
   recipient — the rest of the job still completes, as the brief requires.
"""
from __future__ import annotations

import unicodedata
from datetime import date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_validator, model_validator

from .models import JobStatus, RecipientStatus

MAX_TEXT = 200


def _reject_control_chars(value: str, field: str) -> str:
    for ch in value:
        if unicodedata.category(ch) == "Cc":  # control characters (\n, \t, ...)
            raise ValueError(f"{field} must not contain control characters")
    return value


def _reject_unrenderable(value: str, field: str) -> str:
    """Reject characters the PDF base-14 fonts (WinAnsi/cp1252) cannot draw.

    Applied to *job-wide* fields only: if the course title contained
    Devanagari, EVERY certificate would come out garbled, so the whole
    request is rejected up front. Per-recipient names are checked during
    processing instead, so one bad row cannot block the batch.
    """
    try:
        value.encode("cp1252")
    except UnicodeEncodeError as exc:
        bad = value[exc.start]
        raise ValueError(
            f"{field} contains character {bad!r} (U+{ord(bad):04X}) which the "
            "certificate template font does not support"
        )
    return value


class RecipientIn(BaseModel):
    """One row of the bulk request."""

    full_name: str = Field(
        min_length=1, max_length=MAX_TEXT, examples=["Alice Smith"]
    )
    email: EmailStr | None = Field(default=None, examples=["alice@example.com"])

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = _reject_control_chars(v, "full_name")
        v = " ".join(v.split())  # trim and collapse internal runs of whitespace
        if not v:
            raise ValueError("full_name must contain non-whitespace characters")
        return v


class JobCreate(BaseModel):
    """POST /jobs body: certificate info + the recipient list."""

    course_title: str = Field(
        min_length=1, max_length=MAX_TEXT, examples=["Advanced Python Workshop"]
    )
    issuer_name: str = Field(min_length=1, max_length=MAX_TEXT, examples=["Acme Training Corp"])
    issue_date: date | None = Field(
        default=None,
        description="Date printed on the certificate. Defaults to today (UTC). "
        "May be at most one year in the future (pre-dating an upcoming event).",
    )
    signatory_name: str | None = Field(default=None, max_length=MAX_TEXT, examples=["Jane Doe"])
    signatory_title: str | None = Field(
        default=None, max_length=MAX_TEXT, examples=["Programme Director"]
    )
    recipients: list[RecipientIn] = Field(min_length=1)

    @field_validator("course_title", "issuer_name", "signatory_name", "signatory_title")
    @classmethod
    def _clean_shared_text(cls, v: str | None, info) -> str | None:
        if v is None:
            return None
        v = _reject_control_chars(v, info.field_name).strip()
        if not v:
            raise ValueError(f"{info.field_name} must contain non-whitespace characters")
        return _reject_unrenderable(v, info.field_name)

    @model_validator(mode="after")
    def _check_issue_date(self) -> JobCreate:
        if self.issue_date is not None and self.issue_date > date.today() + timedelta(days=366):
            raise ValueError("issue_date cannot be more than one year in the future")
        return self


class RecipientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    position: int
    full_name: str
    email: str | None
    status: RecipientStatus
    certificate_uid: str | None
    error_message: str | None
    generated_at: datetime | None
    # Filled by the API for generated recipients so clients can download
    # without assembling URLs themselves.
    certificate_url: str | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: JobStatus
    course_title: str
    issuer_name: str
    issue_date: date
    signatory_name: str | None
    signatory_title: str | None
    total_recipients: int
    succeeded_count: int
    failed_count: int
    error: str | None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress_pct(self) -> float:
        """Share of recipients that have reached a final state, 0-100."""
        if self.total_recipients == 0:
            return 0.0
        return round(100.0 * (self.succeeded_count + self.failed_count) / self.total_recipients, 1)


class RecipientPage(BaseModel):
    items: list[RecipientOut]
    total: int
    page: int
    page_size: int


class JobPage(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    page_size: int
