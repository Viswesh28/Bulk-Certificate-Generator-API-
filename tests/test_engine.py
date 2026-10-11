"""Processing-engine tests, run synchronously (no HTTP, no event loop).

These cover the failure-isolation and resume guarantees directly, which are the
most important properties of the system.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.database import make_engine, make_session_factory, init_db
from app.models import CertificateJob, JobStatus, Recipient, RecipientStatus
from app.services import engine as engine_mod
from app.services.certificate import render_certificate_pdf
from app.services.storage import LocalStorage


@pytest.fixture
def env(tmp_path):
    db = make_engine(f"sqlite:///{tmp_path / 'engine.db'}")
    init_db(db)
    factory = make_session_factory(db)
    storage = LocalStorage(tmp_path / "store")
    yield factory, storage
    db.dispose()


def _make_job(factory, names: list[tuple[str, str | None]]) -> str:
    with factory() as s:
        job = CertificateJob(
            course_title="Testing 101",
            issuer_name="Acme",
            issue_date=date(2026, 10, 1),
            total_recipients=len(names),
        )
        job.recipients = [
            Recipient(position=i, full_name=n, email=e, status=RecipientStatus.PENDING)
            for i, (n, e) in enumerate(names)
        ]
        s.add(job)
        s.commit()
        return job.id


def _load(factory, job_id: str):
    with factory() as s:
        job = s.get(CertificateJob, job_id)
        recipients = list(
            s.scalars(select(Recipient).where(Recipient.job_id == job_id).order_by(Recipient.position))
        )
        return job, recipients


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #

def test_all_valid_recipients_generated(env):
    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None), ("Bob Jones", None)])

    engine_mod.process_job(factory, storage, job_id)

    job, recipients = _load(factory, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.succeeded_count == 2 and job.failed_count == 0
    for r in recipients:
        assert r.status == RecipientStatus.GENERATED
        assert r.certificate_uid and r.certificate_uid.startswith("CERT-")
        assert r.file_key == f"{job_id}/{r.id}.pdf"
        assert storage.exists(r.file_key)
        assert storage.read(r.file_key).startswith(b"%PDF-")


def test_certificate_uid_on_record_matches_pdf(env):
    """Regression test: the UID stored in the DB must be the one printed on the PDF."""
    from pypdf import PdfReader
    import io

    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None)])
    engine_mod.process_job(factory, storage, job_id)

    _, [r] = _load(factory, job_id)
    text = PdfReader(io.BytesIO(storage.read(r.file_key))).pages[0].extract_text()
    assert r.certificate_uid in text


def test_recipient_order_is_preserved(env):
    factory, storage = env
    names = [("Zed Zulu", None), ("Amy Adams", None), ("Mia Moss", None)]
    job_id = _make_job(factory, names)
    engine_mod.process_job(factory, storage, job_id)

    _, recipients = _load(factory, job_id)
    assert [r.full_name for r in recipients] == [n for n, _ in names]


# --------------------------------------------------------------------------- #
# failure isolation (required by the brief)
# --------------------------------------------------------------------------- #

def test_single_render_failure_does_not_stop_other_certificates(env, monkeypatch):
    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None), ("Bob Jones", None), ("Carol White", None)])

    def flaky(data):
        if data.recipient_name == "Bob Jones":
            raise RuntimeError("simulated renderer crash")
        return render_certificate_pdf(data)

    monkeypatch.setattr(engine_mod, "render_certificate_pdf", flaky)
    engine_mod.process_job(factory, storage, job_id)

    job, recipients = _load(factory, job_id)
    by_name = {r.full_name: r for r in recipients}
    assert by_name["Alice Smith"].status == RecipientStatus.GENERATED
    assert by_name["Carol White"].status == RecipientStatus.GENERATED
    assert by_name["Bob Jones"].status == RecipientStatus.FAILED
    assert "simulated renderer crash" in by_name["Bob Jones"].error_message
    assert by_name["Bob Jones"].file_key is None
    assert job.status == JobStatus.COMPLETED_WITH_ERRORS
    assert (job.succeeded_count, job.failed_count) == (2, 1)


def test_failed_recipient_leaves_no_orphan_pointer(env, monkeypatch):
    factory, storage = env
    job_id = _make_job(factory, [("Bob Jones", None)])
    monkeypatch.setattr(engine_mod, "render_certificate_pdf", lambda data: 1 / 0)

    engine_mod.process_job(factory, storage, job_id)

    _, [r] = _load(factory, job_id)
    assert r.status == RecipientStatus.FAILED
    assert r.certificate_uid is None and r.file_key is None and r.generated_at is None
    assert "ZeroDivisionError" in r.error_message


def test_storage_failure_for_one_recipient_is_isolated(env, monkeypatch):
    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None), ("Bob Jones", None)])
    real_save = storage.save
    calls = {"n": 0}

    def flaky_save(key, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_save(key, data)

    monkeypatch.setattr(storage, "save", flaky_save)
    engine_mod.process_job(factory, storage, job_id)

    job, recipients = _load(factory, job_id)
    statuses = sorted(r.status.value for r in recipients)
    assert statuses == ["failed", "generated"]
    assert job.status == JobStatus.COMPLETED_WITH_ERRORS


def test_validation_failures_are_per_recipient(env):
    factory, storage = env
    job_id = _make_job(
        factory,
        [
            ("Alice Smith", "a@example.com"),
            ("Z", None),                       # too short
            ("Chen 陈", None),                 # not renderable in cp1252
            ("Alice Smith", "a@example.com"),  # duplicate of position 0
            ("Bob Jones", None),
        ],
    )
    engine_mod.process_job(factory, storage, job_id)

    job, recipients = _load(factory, job_id)
    assert [r.status.value for r in recipients] == [
        "generated", "failed", "failed", "failed", "generated",
    ]
    assert "at least 2 characters" in recipients[1].error_message
    assert "cp1252" in recipients[2].error_message or "font" in recipients[2].error_message
    assert "duplicate" in recipients[3].error_message
    assert job.status == JobStatus.COMPLETED_WITH_ERRORS


def test_failed_row_does_not_block_later_valid_duplicate(env, monkeypatch):
    """A row that fails rendering must not poison duplicate detection."""
    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None), ("Alice Smith", None)])

    calls = {"n": 0}

    def fail_first(data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return render_certificate_pdf(data)

    monkeypatch.setattr(engine_mod, "render_certificate_pdf", fail_first)
    engine_mod.process_job(factory, storage, job_id)

    _, recipients = _load(factory, job_id)
    assert [r.status.value for r in recipients] == ["failed", "generated"]


def test_all_recipients_failing_marks_job_failed(env, monkeypatch):
    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None), ("Bob Jones", None)])
    monkeypatch.setattr(engine_mod, "render_certificate_pdf", lambda data: 1 / 0)

    engine_mod.process_job(factory, storage, job_id)

    job, _ = _load(factory, job_id)
    assert job.status == JobStatus.FAILED
    assert (job.succeeded_count, job.failed_count) == (0, 2)


def test_fatal_job_error_is_recorded_not_raised(env, monkeypatch):
    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None)])

    def explode(*_args, **_kwargs):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(engine_mod, "_process_job", explode)
    engine_mod.process_job(factory, storage, job_id)  # must not raise

    job, _ = _load(factory, job_id)
    assert job.status == JobStatus.FAILED
    assert "database on fire" in job.error


# --------------------------------------------------------------------------- #
# idempotency and crash recovery
# --------------------------------------------------------------------------- #

def test_processing_a_terminal_job_is_a_noop(env, monkeypatch):
    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None)])
    engine_mod.process_job(factory, storage, job_id)

    calls = {"n": 0}

    def counting(data):
        calls["n"] += 1
        return render_certificate_pdf(data)

    monkeypatch.setattr(engine_mod, "render_certificate_pdf", counting)
    engine_mod.process_job(factory, storage, job_id)

    assert calls["n"] == 0


def test_resume_does_not_regenerate_finished_certificates(env, monkeypatch):
    """Simulate a crash after the first recipient: only the rest should render."""
    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None), ("Bob Jones", None)])

    # Pretend Alice was completed by an earlier, crashed run.
    with factory() as s:
        r = s.scalars(select(Recipient).where(Recipient.full_name == "Alice Smith")).one()
        storage.save(f"{job_id}/{r.id}.pdf", render_certificate_pdf(_dummy_data("Alice Smith")))
        r.status = RecipientStatus.GENERATED
        r.file_key = f"{job_id}/{r.id}.pdf"
        r.certificate_uid = "CERT-OLD"
        s.commit()

    rendered = []

    def track(data):
        rendered.append(data.recipient_name)
        return render_certificate_pdf(data)

    monkeypatch.setattr(engine_mod, "render_certificate_pdf", track)
    engine_mod.process_job(factory, storage, job_id)

    assert rendered == ["Bob Jones"]
    _, recipients = _load(factory, job_id)
    alice = next(r for r in recipients if r.full_name == "Alice Smith")
    assert alice.certificate_uid == "CERT-OLD"  # untouched


def test_resume_requeues_recipients_stuck_in_processing(env):
    factory, storage = env
    job_id = _make_job(factory, [("Alice Smith", None)])
    with factory() as s:
        job = s.get(CertificateJob, job_id)
        job.status = JobStatus.PROCESSING
        job.recipients[0].status = RecipientStatus.PROCESSING
        s.commit()

    engine_mod.process_job(factory, storage, job_id)

    job, [r] = _load(factory, job_id)
    assert r.status == RecipientStatus.GENERATED
    assert job.status == JobStatus.COMPLETED


def test_find_resumable_jobs_returns_only_unfinished(env):
    factory, storage = env
    done = _make_job(factory, [("Alice Smith", None)])
    engine_mod.process_job(factory, storage, done)
    pending = _make_job(factory, [("Bob Jones", None)])

    assert engine_mod.find_resumable_jobs(factory) == [pending]


def _dummy_data(name: str):
    from app.services.certificate import CertificateData

    return CertificateData(
        recipient_name=name,
        course_title="Testing 101",
        issuer_name="Acme",
        issue_date=date(2026, 10, 1),
        signatory_name=None,
        signatory_title=None,
        certificate_uid="CERT-DUMMY",
    )
