"""Unit tests for validation rules, schemas, storage and the certificate renderer."""
from __future__ import annotations

import io
from datetime import date

import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from app.schemas import JobCreate, RecipientIn
from app.services.certificate import CertificateData, format_issue_date, render_certificate_pdf
from app.services.storage import LocalStorage
from app.services.validation import RecipientValidationError, recipient_key, validate_recipient
from app.util import slugify


# --------------------------------------------------------------------------- #
# per-recipient validation (engine tier)
# --------------------------------------------------------------------------- #

def test_valid_recipient_passes():
    validate_recipient("Alice Smith", "a@example.com", {})


@pytest.mark.parametrize("name", ["A", "   ", " B "])
def test_too_short_names_are_rejected(name):
    with pytest.raises(RecipientValidationError, match="at least 2"):
        validate_recipient(name, None, {})


def test_control_characters_rejected():
    with pytest.raises(RecipientValidationError, match="control"):
        validate_recipient("Ali\tce", None, {})


def test_unrenderable_characters_rejected_with_codepoint():
    with pytest.raises(RecipientValidationError, match="U\\+6F22"):
        validate_recipient("漢字 Name", None, {})


def test_duplicate_detection_is_case_insensitive_and_email_aware():
    seen = {recipient_key("Alice Smith", "a@x.com"): 0}

    with pytest.raises(RecipientValidationError, match="duplicate"):
        validate_recipient("alice smith", "A@X.COM", seen)

    # same name, different email -> not a duplicate
    validate_recipient("Alice Smith", "other@x.com", seen)


def test_validate_does_not_mutate_seen_map():
    seen: dict = {}
    validate_recipient("Alice Smith", None, seen)
    assert seen == {}


# --------------------------------------------------------------------------- #
# request schema (API tier)
# --------------------------------------------------------------------------- #

def test_recipient_schema_strips_and_collapses_whitespace():
    assert RecipientIn(full_name="  Ada   Lovelace ").full_name == "Ada Lovelace"


def test_job_schema_strips_shared_text():
    job = JobCreate(course_title="  Course  ", issuer_name="Org", recipients=[{"full_name": "Ann"}])
    assert job.course_title == "Course"


def test_job_schema_rejects_empty_recipients():
    with pytest.raises(ValidationError):
        JobCreate(course_title="C", issuer_name="I", recipients=[])


def test_job_schema_rejects_bad_email_in_recipient():
    with pytest.raises(ValidationError):
        RecipientIn(full_name="Ann", email="nope")


def test_job_schema_allows_issue_date_within_a_year():
    near = date.today().replace(year=date.today().year + 1)
    JobCreate(course_title="C", issuer_name="I", issue_date=near, recipients=[{"full_name": "Ann"}])


# --------------------------------------------------------------------------- #
# certificate renderer
# --------------------------------------------------------------------------- #

def _data(**overrides) -> CertificateData:
    base = dict(
        recipient_name="Alice Smith",
        course_title="Advanced Python Workshop",
        issuer_name="Acme Training Corp",
        issue_date=date(2026, 10, 1),
        signatory_name="Jane Doe",
        signatory_title="Programme Director",
        certificate_uid="CERT-TEST00000001",
    )
    base.update(overrides)
    return CertificateData(**base)


def _text(pdf: bytes) -> str:
    return PdfReader(io.BytesIO(pdf)).pages[0].extract_text()


def test_renderer_produces_single_page_landscape_pdf():
    pdf = render_certificate_pdf(_data())
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    page = reader.pages[0]
    assert float(page.mediabox.width) > float(page.mediabox.height)  # landscape


def test_renderer_contains_recipient_specific_fields():
    text = _text(render_certificate_pdf(_data()))
    # The issuer is printed in capitals as the letterhead, hence .upper().
    for expected in ["Alice Smith", "Advanced Python Workshop", "ACME TRAINING CORP",
                     "Jane Doe", "Programme Director", "1 October 2026", "CERT-TEST00000001"]:
        assert expected in text, expected


def test_renderer_handles_very_long_name_without_crashing():
    long_name = "Maximilian " * 15 + "Smith"
    pdf = render_certificate_pdf(_data(recipient_name=long_name.strip()))
    assert pdf.startswith(b"%PDF-")


def test_renderer_handles_missing_signatory():
    pdf = render_certificate_pdf(_data(signatory_name=None, signatory_title=None))
    assert "Authorised Signatory" in _text(pdf)


def test_format_issue_date_has_no_leading_zero():
    assert format_issue_date(date(2026, 3, 5)) == "5 March 2026"


def test_slugify_produces_safe_ascii():
    assert slugify("Zoë Ñúñez / ../etc") == "zoe-nunez-etc"
    assert slugify("!!!") == "certificate"


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def test_storage_round_trip(tmp_path):
    storage = LocalStorage(tmp_path)
    storage.save("job/rec.pdf", b"%PDF-data")
    assert storage.exists("job/rec.pdf")
    assert storage.read("job/rec.pdf") == b"%PDF-data"


@pytest.mark.parametrize("key", ["../escape.pdf", "/etc/passwd", "a/../../b.pdf", ""])
def test_storage_rejects_path_traversal(tmp_path, key):
    storage = LocalStorage(tmp_path / "root")
    with pytest.raises(ValueError):
        storage.save(key, b"x")
