"""ORM models.

Two tables:

* ``certificate_jobs`` – one row per bulk request (the "job"): the shared
  certificate information plus aggregate status/counters.
* ``recipients``      – one row per certificate: the per-recipient input,
  its individual status, error message (if any) and storage key (if done).

Job status semantics (see README):
    queued ──▶ processing ──▶ completed                (all succeeded)
                          └─▶ completed_with_errors    (some failed)
                          └─▶ failed                   (all failed / fatal)
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .util import utcnow


def _uuid() -> str:
    return str(uuid.uuid4())


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED}


class RecipientStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    GENERATED = "generated"
    FAILED = "failed"


def _enum_column(enum_cls):
    """Store the enum *values* ("queued"), not the member names ("QUEUED")."""
    return Enum(enum_cls, values_callable=lambda e: [m.value for m in e])


class CertificateJob(Base):
    __tablename__ = "certificate_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    # --- certificate template inputs (shared by every recipient) -----------
    course_title: Mapped[str] = mapped_column(String(200), nullable=False)
    issuer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    signatory_name: Mapped[str | None] = mapped_column(String(200))
    signatory_title: Mapped[str | None] = mapped_column(String(200))

    # --- progress tracking -------------------------------------------------
    status: Mapped[JobStatus] = mapped_column(
        _enum_column(JobStatus), default=JobStatus.QUEUED, nullable=False, index=True
    )
    total_recipients: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Free-text field for *job-level* fatal errors (not per-recipient ones).
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    recipients: Mapped[list[Recipient]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="Recipient.position",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CertificateJob {self.id} {self.status.value} {self.succeeded_count}/{self.total_recipients}>"


class Recipient(Base):
    __tablename__ = "recipients"
    __table_args__ = (Index("ix_recipients_job_position", "job_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("certificate_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Preserves the order the client submitted recipients in.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- input --------------------------------------------------------------
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))

    # --- output / per-item status -------------------------------------------
    status: Mapped[RecipientStatus] = mapped_column(
        _enum_column(RecipientStatus), default=RecipientStatus.PENDING, nullable=False, index=True
    )
    # Printed on the certificate and unique across the system; can be used
    # later for a public verification endpoint.
    certificate_uid: Mapped[str | None] = mapped_column(String(40), unique=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    # Storage-*relative* key (e.g. "<job_id>/<recipient_id>.pdf"), so the
    # storage backend can be swapped without touching the database.
    file_key: Mapped[str | None] = mapped_column(String(300))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    job: Mapped[CertificateJob] = relationship(back_populates="recipients")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Recipient {self.full_name!r} {self.status.value}>"
