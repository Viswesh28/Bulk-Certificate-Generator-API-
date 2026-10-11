"""Job processing engine — the heart of the system.

Pure orchestration: **no FastAPI imports**. Everything is passed in
(``session_factory``, ``storage``, ``job_id``), so the engine can run

* in a worker thread of this process (default, see ``worker.py``),
* directly in a test (synchronously, no event loop needed), or
* inside a Celery/RQ task later, by moving this one function.

Failure-isolation contract required by the brief:
    "A failure while generating one certificate should not unnecessarily
     prevent other valid certificates in the same job from being generated."

Every recipient is processed in its own try/except; failures are recorded on
the recipient row (status=failed + error_message) and the loop continues.
Job-level counters are *recomputed from the recipients table* at the end
rather than incremented along the way — that makes finalisation correct even
if an individual step crashed mid-transaction.
"""
from __future__ import annotations

import logging
import uuid
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CertificateJob, JobStatus, Recipient, RecipientStatus
from ..util import utcnow
from .certificate import CertificateData, render_certificate_pdf
from .storage import LocalStorage
from .validation import RecipientValidationError, recipient_key, validate_recipient

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
MAX_ERROR_LEN = 1000


def new_certificate_uid() -> str:
    """Unique, human-readable id printed on the certificate (12 hex chars)."""
    return "CERT-" + uuid.uuid4().hex[:12].upper()


def process_job(session_factory: SessionFactory, storage: LocalStorage, job_id: str) -> None:
    """Process one job to a terminal state. Never raises.

    Safe to call twice: jobs already in a terminal state are skipped, and
    recipients already GENERATED are not re-rendered (crash-resume).
    """
    try:
        _process_job(session_factory, storage, job_id)
    except Exception as exc:  # fatal, job-level failure (e.g. storage unwritable)
        logger.exception("job %s failed fatally", job_id)
        _record_fatal_error(session_factory, job_id, f"{type(exc).__name__}: {exc}")


def find_resumable_jobs(session_factory: SessionFactory) -> list[str]:
    """Jobs left queued/processing by a crash or shutdown — re-queue on boot."""
    with session_factory() as session:
        return list(
            session.scalars(
                select(CertificateJob.id).where(
                    CertificateJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING])
                )
            ).all()
        )


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

def _process_job(session_factory: SessionFactory, storage: LocalStorage, job_id: str) -> None:
    with session_factory() as session:
        job = session.get(CertificateJob, job_id)
        if job is None:
            logger.warning("job %s no longer exists; nothing to do", job_id)
            return
        if job.status.is_terminal:
            logger.info("job %s already %s; skipping", job_id, job.status.value)
            return

        job.status = JobStatus.PROCESSING
        job.error = None
        # Recipients stuck in 'processing' by a crashed earlier attempt go
        # back to pending so this run picks them up.
        for r in session.scalars(
            select(Recipient).where(
                Recipient.job_id == job_id,
                Recipient.status == RecipientStatus.PROCESSING,
            )
        ):
            r.status = RecipientStatus.PENDING
        session.commit()

        recipients = list(
            session.scalars(
                select(Recipient)
                .where(Recipient.job_id == job_id)
                .order_by(Recipient.position)
            )
        )

        # Duplicate-detection state: pre-seeded with already-generated rows
        # so resumed jobs still catch duplicates of earlier successes.
        seen: dict[tuple[str, str], int] = {
            recipient_key(r.full_name, r.email): r.position
            for r in recipients
            if r.status == RecipientStatus.GENERATED
        }

        for recipient in recipients:
            if recipient.status == RecipientStatus.GENERATED:
                continue  # resume: never regenerate a finished certificate
            try:
                _process_recipient(session, storage, job, recipient, seen)
            except Exception:
                # Last-resort isolation: even a database hiccup on one row
                # must not abort the remaining recipients. The row is swept
                # to FAILED in _finalize_job if it is still non-terminal.
                logger.exception(
                    "unhandled error while processing recipient %s of job %s",
                    recipient.id, job_id,
                )

        _finalize_job(session, job)


def _process_recipient(
    session: Session,
    storage: LocalStorage,
    job: CertificateJob,
    recipient: Recipient,
    seen: dict[tuple[str, str], int],
) -> None:
    # Mark PROCESSING first so the status API shows real-time progress.
    recipient.status = RecipientStatus.PROCESSING
    session.commit()

    try:
        validate_recipient(recipient.full_name, recipient.email, seen)

        # One UID, used both on the PDF and in the database, so a certificate
        # can always be matched back to its recipient row.
        certificate_uid = new_certificate_uid()
        pdf_bytes = render_certificate_pdf(
            CertificateData(
                recipient_name=recipient.full_name.strip(),
                course_title=job.course_title,
                issuer_name=job.issuer_name,
                issue_date=job.issue_date,
                signatory_name=job.signatory_name,
                signatory_title=job.signatory_title,
                certificate_uid=certificate_uid,
            )
        )
        key = f"{job.id}/{recipient.id}.pdf"
        storage.save(key, pdf_bytes)

        recipient.certificate_uid = certificate_uid
        recipient.file_key = key
        recipient.status = RecipientStatus.GENERATED
        recipient.error_message = None
        recipient.generated_at = utcnow()
        session.commit()
        # Register only now that the certificate exists (see validate_recipient).
        seen[recipient_key(recipient.full_name, recipient.email)] = recipient.position
    except RecipientValidationError as exc:
        _mark_failed(session, recipient, f"validation: {exc}")
    except Exception as exc:
        logger.exception("generation failed for recipient %s", recipient.id)
        _mark_failed(session, recipient, f"{type(exc).__name__}: {exc}")


def _mark_failed(session: Session, recipient: Recipient, message: str) -> None:
    # Roll back any partial writes from the failed attempt (e.g. the
    # PROCESSING flag) before recording the failure itself.
    session.rollback()
    recipient.status = RecipientStatus.FAILED
    recipient.error_message = message[:MAX_ERROR_LEN]
    recipient.certificate_uid = None
    recipient.file_key = None
    recipient.generated_at = None
    session.commit()


def _finalize_job(session: Session, job: CertificateJob) -> None:
    # Safety net: after the full loop, nothing may remain pending/processing.
    leftovers = session.scalars(
        select(Recipient).where(
            Recipient.job_id == job.id,
            Recipient.status.in_([RecipientStatus.PENDING, RecipientStatus.PROCESSING]),
        )
    ).all()
    for r in leftovers:
        r.status = RecipientStatus.FAILED
        r.error_message = (r.error_message or "processing interrupted")[:MAX_ERROR_LEN]
    session.commit()

    # Recompute counters from the source of truth (the recipients table).
    rows = session.execute(
        select(Recipient.status, func.count())
        .where(Recipient.job_id == job.id)
        .group_by(Recipient.status)
    ).all()
    counts = {status: count for status, count in rows}

    job.succeeded_count = counts.get(RecipientStatus.GENERATED, 0)
    job.failed_count = counts.get(RecipientStatus.FAILED, 0)
    if job.failed_count == 0:
        job.status = JobStatus.COMPLETED
    elif job.succeeded_count == 0:
        job.status = JobStatus.FAILED
    else:
        job.status = JobStatus.COMPLETED_WITH_ERRORS
    session.commit()
    logger.info(
        "job %s finished: %s (%d ok, %d failed)",
        job.id, job.status.value, job.succeeded_count, job.failed_count,
    )


def _record_fatal_error(session_factory: SessionFactory, job_id: str, message: str) -> None:
    try:
        with session_factory() as session:
            job = session.get(CertificateJob, job_id)
            if job is not None and not job.status.is_terminal:
                job.status = JobStatus.FAILED
                job.error = message[:MAX_ERROR_LEN]
                session.commit()
    except Exception:  # pragma: no cover - truly nothing left to do
        logger.exception("could not record fatal error for job %s", job_id)
