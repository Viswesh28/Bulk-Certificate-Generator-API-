"""HTTP-level tests: job creation, input validation, status, listing, downloads."""
from __future__ import annotations

import io
import zipfile

from pypdf import PdfReader

from tests.conftest import API, wait_for_terminal


def _pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(content)).pages)


# --------------------------------------------------------------------------- #
# creating a job
# --------------------------------------------------------------------------- #

def test_create_job_returns_202_and_location(client, valid_payload):
    response = client.post(f"{API}/certificate-jobs", json=valid_payload)

    assert response.status_code == 202
    body = response.json()
    assert body["id"]
    assert response.headers["location"] == f"{API}/certificate-jobs/{body['id']}"
    assert body["total_recipients"] == 3
    assert body["course_title"] == "Advanced Python Workshop"
    assert body["issue_date"] == "2026-10-01"


def test_create_job_defaults_issue_date_to_today(client, valid_payload):
    payload = {k: v for k, v in valid_payload.items() if k != "issue_date"}
    body = client.post(f"{API}/certificate-jobs", json=payload).json()
    assert body["issue_date"]  # populated, not null


def test_job_is_processed_to_completion(client, valid_payload):
    job_id = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]

    job = wait_for_terminal(client, job_id)

    assert job["status"] == "completed"
    assert job["succeeded_count"] == 3
    assert job["failed_count"] == 0
    assert job["progress_pct"] == 100.0


def test_unknown_job_returns_404(client):
    response = client.get(f"{API}/certificate-jobs/does-not-exist")
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# input validation (whole-request rejections -> 422)
# --------------------------------------------------------------------------- #

def _post_with(client, valid_payload, **overrides):
    return client.post(f"{API}/certificate-jobs", json={**valid_payload, **overrides})


def test_rejects_empty_recipient_list(client, valid_payload):
    assert _post_with(client, valid_payload, recipients=[]).status_code == 422


def test_rejects_missing_course_title(client, valid_payload):
    payload = {k: v for k, v in valid_payload.items() if k != "course_title"}
    assert client.post(f"{API}/certificate-jobs", json=payload).status_code == 422


def test_rejects_blank_course_title(client, valid_payload):
    assert _post_with(client, valid_payload, course_title="   ").status_code == 422


def test_rejects_course_title_with_unrenderable_characters(client, valid_payload):
    response = _post_with(client, valid_payload, course_title="Python 漢字")
    assert response.status_code == 422
    assert "course_title" in response.text


def test_rejects_invalid_email_with_row_location(client, valid_payload):
    valid_payload["recipients"][1]["email"] = "not-an-email"
    response = client.post(f"{API}/certificate-jobs", json=valid_payload)

    assert response.status_code == 422
    locations = [tuple(e["loc"]) for e in response.json()["detail"]]
    assert any("recipients" in loc and 1 in loc and "email" in loc for loc in locations)


def test_rejects_control_characters_in_name(client, valid_payload):
    valid_payload["recipients"][0]["full_name"] = "Alice\nSmith"
    assert client.post(f"{API}/certificate-jobs", json=valid_payload).status_code == 422


def test_rejects_name_longer_than_limit(client, valid_payload):
    valid_payload["recipients"][0]["full_name"] = "A" * 201
    assert client.post(f"{API}/certificate-jobs", json=valid_payload).status_code == 422


def test_rejects_issue_date_far_in_future(client, valid_payload):
    assert _post_with(client, valid_payload, issue_date="2999-01-01").status_code == 422


def test_rejects_malformed_json_body(client):
    response = client.post(
        f"{API}/certificate-jobs",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422


def test_rejects_too_many_recipients(client, valid_payload, settings):
    row = {"full_name": "Filler Person"}
    valid_payload["recipients"] = [row] * (settings.max_recipients_per_job + 1)
    response = client.post(f"{API}/certificate-jobs", json=valid_payload)

    assert response.status_code == 422
    assert "too many recipients" in response.json()["detail"]


def test_names_are_trimmed_and_whitespace_collapsed(client, valid_payload):
    valid_payload["recipients"] = [{"full_name": "  Bob    Jones  "}]
    job_id = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]
    wait_for_terminal(client, job_id)

    item = client.get(f"{API}/certificate-jobs/{job_id}/recipients").json()["items"][0]
    assert item["full_name"] == "Bob Jones"


# --------------------------------------------------------------------------- #
# status, progress and listing
# --------------------------------------------------------------------------- #

def test_progress_is_reported_for_terminal_job(client, valid_payload):
    job_id = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]
    job = wait_for_terminal(client, job_id)
    assert job["succeeded_count"] + job["failed_count"] == job["total_recipients"]


def test_list_jobs_is_paginated_newest_first(client, valid_payload):
    ids = [client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"] for _ in range(3)]

    page1 = client.get(f"{API}/certificate-jobs", params={"page": 1, "page_size": 2}).json()
    page2 = client.get(f"{API}/certificate-jobs", params={"page": 2, "page_size": 2}).json()

    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    returned = [j["id"] for j in page1["items"] + page2["items"]]
    assert set(returned) == set(ids)


def test_list_recipients_pagination_and_status_filter(client, valid_payload):
    job_id = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]
    wait_for_terminal(client, job_id)

    page = client.get(
        f"{API}/certificate-jobs/{job_id}/recipients", params={"page": 1, "page_size": 2}
    ).json()
    assert page["total"] == 3
    assert [r["position"] for r in page["items"]] == [0, 1]

    failed = client.get(
        f"{API}/certificate-jobs/{job_id}/recipients", params={"status": "failed"}
    ).json()
    assert failed["total"] == 0


def test_list_recipients_for_unknown_job_is_404(client):
    assert client.get(f"{API}/certificate-jobs/nope/recipients").status_code == 404


# --------------------------------------------------------------------------- #
# retrieving certificates
# --------------------------------------------------------------------------- #

def test_download_single_certificate_pdf(client, valid_payload):
    job_id = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]
    wait_for_terminal(client, job_id)
    recipient = client.get(f"{API}/certificate-jobs/{job_id}/recipients").json()["items"][0]

    response = client.get(recipient["certificate_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    text = _pdf_text(response.content)
    assert "Alice Smith" in text
    assert "Advanced Python Workshop" in text
    assert "ACME TRAINING CORP" in text  # letterhead is uppercased
    assert recipient["certificate_uid"] in text


def test_certificate_url_is_absent_for_pending_or_failed_recipient(client, valid_payload, monkeypatch):
    from app.services import engine

    def boom(data):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(engine, "render_certificate_pdf", boom)
    job_id = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]
    wait_for_terminal(client, job_id)

    items = client.get(f"{API}/certificate-jobs/{job_id}/recipients").json()["items"]
    assert all(item["certificate_url"] is None for item in items)


def test_download_certificate_for_failed_recipient_is_409(client, valid_payload, monkeypatch):
    from app.services import engine

    real_render = engine.render_certificate_pdf

    def selective(data):
        if data.recipient_name == "Bob Jones":
            raise RuntimeError("font exploded")
        return real_render(data)

    monkeypatch.setattr(engine, "render_certificate_pdf", selective)
    job_id = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]
    wait_for_terminal(client, job_id)

    failed = client.get(
        f"{API}/certificate-jobs/{job_id}/recipients", params={"status": "failed"}
    ).json()["items"][0]
    response = client.get(
        f"{API}/certificate-jobs/{job_id}/recipients/{failed['id']}/certificate"
    )
    assert response.status_code == 409


def test_download_certificate_wrong_job_is_404(client, valid_payload):
    job_a = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]
    job_b = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]
    wait_for_terminal(client, job_a)
    wait_for_terminal(client, job_b)
    recipient_a = client.get(f"{API}/certificate-jobs/{job_a}/recipients").json()["items"][0]

    response = client.get(f"/api/v1/certificate-jobs/{job_b}/recipients/{recipient_a['id']}/certificate")
    assert response.status_code == 404


def test_download_all_certificates_as_zip(client, valid_payload):
    job_id = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]
    wait_for_terminal(client, job_id)

    response = client.get(f"{API}/certificate-jobs/{job_id}/certificates.zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert len(names) == 3
        assert all(n.endswith(".pdf") for n in names)
        assert len(set(names)) == 3  # no collisions
        assert archive.read(names[0]).startswith(b"%PDF-")


def test_zip_is_409_while_job_still_running(client, valid_payload, monkeypatch):
    from app.services import worker as worker_mod

    # Hold the job in "queued" by making the worker's submit a no-op.
    monkeypatch.setattr(worker_mod.BackgroundWorker, "submit", lambda self, job_id: None)
    job_id = client.post(f"{API}/certificate-jobs", json=valid_payload).json()["id"]

    response = client.get(f"{API}/certificate-jobs/{job_id}/certificates.zip")
    assert response.status_code == 409


def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_root_serves_the_register_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Certificate Register" in response.text


def test_api_responses_allow_cross_origin_browser_calls(client):
    response = client.get(f"{API}/certificate-jobs", headers={"Origin": "null"})
    assert response.headers["access-control-allow-origin"] == "*"
