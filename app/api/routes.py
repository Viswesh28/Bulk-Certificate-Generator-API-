"""REST endpoints.

    POST   /certificate-jobs                                   submit a bulk request  -> 202
    GET    /certificate-jobs                                   list jobs (paginated)
    GET    /certificate-jobs/{job_id}                          job status + progress
    GET    /certificate-jobs/{job_id}/recipients               per-recipient results
    GET    /certificate-jobs/{job_id}/recipients/{rid}/certificate   one PDF
    GET    /certificate-jobs/{job_id}/certificates.zip         all generated PDFs
"""
from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import CertificateJob, Recipient, RecipientStatus
from ..schemas import JobCreate, JobOut, JobPage, RecipientOut, RecipientPage
from ..services import jobs as job_service
from ..services.storage import LocalStorage
from ..services.worker import BackgroundWorker
from ..util import slugify
from .deps import get_session, get_settings, get_storage, get_worker

router = APIRouter()

Page = Query(default=1, ge=1)
PageSize = Query(default=50, ge=1, le=200)


def _certificate_url(request: Request, job_id: str, recipient_id: str) -> str:
    return str(
        request.url_for("download_certificate", job_id=job_id, recipient_id=recipient_id)
    )


def _recipient_out(request: Request, job_id: str, r: Recipient) -> RecipientOut:
    out = RecipientOut.model_validate(r)
    if r.status == RecipientStatus.GENERATED:
        out.certificate_url = _certificate_url(request, job_id, r.id)
    return out


def _get_job_or_404(session: Session, job_id: str) -> CertificateJob:
    job = session.get(CertificateJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"job {job_id} not found")
    return job


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #

@router.post(
    "/certificate-jobs",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a bulk certificate generation job",
    responses={422: {"description": "Invalid request body or too many recipients"}},
)
def create_job(
    payload: JobCreate,
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    worker: BackgroundWorker = Depends(get_worker),
) -> JobOut:
    if len(payload.recipients) > settings.max_recipients_per_job:
        raise HTTPException(
            422,
            detail=(
                f"too many recipients: {len(payload.recipients)} > "
                f"{settings.max_recipients_per_job}; split the list into several jobs"
            ),
        )

    job = job_service.create_job(session, payload)
    # Hand off to the background worker; the response returns immediately.
    worker.submit(job.id)

    response.headers["Location"] = f"{settings.api_v1_prefix}/certificate-jobs/{job.id}"
    return JobOut.model_validate(job)


@router.get("/certificate-jobs", response_model=JobPage, summary="List jobs, newest first")
def list_jobs(
    page: int = Page,
    page_size: int = PageSize,
    session: Session = Depends(get_session),
) -> JobPage:
    items, total = job_service.list_jobs(session, page, page_size)
    return JobPage(
        items=[JobOut.model_validate(j) for j in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/certificate-jobs/{job_id}",
    response_model=JobOut,
    summary="Job status, progress and counters",
    responses={404: {"description": "Unknown job"}},
)
def get_job(job_id: str, session: Session = Depends(get_session)) -> JobOut:
    return JobOut.model_validate(_get_job_or_404(session, job_id))


# --------------------------------------------------------------------------- #
# recipients & certificates
# --------------------------------------------------------------------------- #

@router.get(
    "/certificate-jobs/{job_id}/recipients",
    response_model=RecipientPage,
    summary="Per-recipient status, errors and download links",
    responses={404: {"description": "Unknown job"}},
)
def list_recipients(
    job_id: str,
    request: Request,
    page: int = Page,
    page_size: int = PageSize,
    status_filter: RecipientStatus | None = Query(
        default=None, alias="status", description="Only return recipients in this state"
    ),
    session: Session = Depends(get_session),
) -> RecipientPage:
    _get_job_or_404(session, job_id)
    items, total = job_service.list_recipients(session, job_id, page, page_size, status_filter)
    return RecipientPage(
        items=[_recipient_out(request, job_id, r) for r in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/certificate-jobs/{job_id}/recipients/{recipient_id}/certificate",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The certificate PDF"},
        404: {"description": "Unknown job or recipient"},
        409: {"description": "Certificate not generated (pending or failed)"},
    },
    summary="Download one generated certificate (PDF)",
    name="download_certificate",
)
def download_certificate(
    job_id: str,
    recipient_id: str,
    session: Session = Depends(get_session),
    storage: LocalStorage = Depends(get_storage),
):
    _get_job_or_404(session, job_id)
    recipient = session.get(Recipient, recipient_id)
    if recipient is None or recipient.job_id != job_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="recipient not found in this job")
    if recipient.status != RecipientStatus.GENERATED or not recipient.file_key:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"certificate not available (status: {recipient.status.value})",
        )
    path = storage.abspath(recipient.file_key)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="certificate file is missing")
    filename = f"certificate-{slugify(recipient.full_name)}-{recipient.certificate_uid}.pdf"
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.get(
    "/certificate-jobs/{job_id}/certificates.zip",
    response_class=Response,
    responses={
        200: {"content": {"application/zip": {}}, "description": "ZIP of all generated PDFs"},
        404: {"description": "Unknown job"},
        409: {"description": "Job has no generated certificates yet"},
    },
    summary="Download every generated certificate for a job as a ZIP",
)
def download_all_certificates(
    job_id: str,
    session: Session = Depends(get_session),
    storage: LocalStorage = Depends(get_storage),
) -> Response:
    job = _get_job_or_404(session, job_id)
    if not job.status.is_terminal:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"job is still {job.status.value}; try again later"
        )
    generated = job_service.generated_recipients(session, job_id)
    if not generated:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="job has no generated certificates")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for r in generated:
            name = f"{slugify(r.full_name)}-{r.certificate_uid}.pdf"
            archive.writestr(name, storage.read(r.file_key))

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="certificates-{job_id}.zip"'},
    )
