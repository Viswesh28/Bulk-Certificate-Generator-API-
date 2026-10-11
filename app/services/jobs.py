"""Job-level database operations used by the API layer.

Kept separate from the routes so the HTTP code stays thin and these functions
can be exercised directly in tests.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CertificateJob, Recipient, RecipientStatus
from ..schemas import JobCreate
from ..util import utcnow


def create_job(session: Session, payload: JobCreate) -> CertificateJob:
    """Persist a job and all its recipients in ONE transaction.

    Either the whole request is stored or none of it is, so the worker can
    never observe a half-written job.
    """
    job = CertificateJob(
        course_title=payload.course_title,
        issuer_name=payload.issuer_name,
        issue_date=payload.issue_date or utcnow().date(),
        signatory_name=payload.signatory_name,
        signatory_title=payload.signatory_title,
        total_recipients=len(payload.recipients),
    )
    job.recipients = [
        Recipient(
            position=index,
            full_name=item.full_name,
            email=str(item.email) if item.email else None,
            status=RecipientStatus.PENDING,
        )
        for index, item in enumerate(payload.recipients)
    ]
    session.add(job)
    session.commit()
    return job


def list_jobs(session: Session, page: int, page_size: int) -> tuple[list[CertificateJob], int]:
    total = session.scalar(select(func.count()).select_from(CertificateJob)) or 0
    items = list(
        session.scalars(
            select(CertificateJob)
            .order_by(CertificateJob.created_at.desc(), CertificateJob.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return items, total


def list_recipients(
    session: Session,
    job_id: str,
    page: int,
    page_size: int,
    status: RecipientStatus | None,
) -> tuple[list[Recipient], int]:
    base = select(Recipient).where(Recipient.job_id == job_id)
    if status is not None:
        base = base.where(Recipient.status == status)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    items = list(
        session.scalars(
            base.order_by(Recipient.position).offset((page - 1) * page_size).limit(page_size)
        )
    )
    return items, total


def generated_recipients(session: Session, job_id: str) -> list[Recipient]:
    return list(
        session.scalars(
            select(Recipient)
            .where(Recipient.job_id == job_id, Recipient.status == RecipientStatus.GENERATED)
            .order_by(Recipient.position)
        )
    )
