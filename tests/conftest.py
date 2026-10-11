"""Shared fixtures.

Every test gets its own temporary SQLite database and storage directory, so
tests are independent and can run in any order.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TERMINAL = {"completed", "completed_with_errors", "failed"}
API = "/api/v1"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=tmp_path / "storage",
        worker_concurrency=2,
        max_recipients_per_job=50,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """HTTP client with the real background worker running (lifespan enabled)."""
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.app_settings = settings  # convenience for tests
        yield test_client


@pytest.fixture
def valid_payload() -> dict:
    return {
        "course_title": "Advanced Python Workshop",
        "issuer_name": "Acme Training Corp",
        "issue_date": "2026-10-01",
        "signatory_name": "Jane Doe",
        "signatory_title": "Programme Director",
        "recipients": [
            {"full_name": "Alice Smith", "email": "alice@example.com"},
            {"full_name": "Bob Jones", "email": "bob@example.com"},
            {"full_name": "Carol White"},
        ],
    }


def wait_for_terminal(client: TestClient, job_id: str, timeout: float = 20.0) -> dict:
    """Poll the status endpoint until the job reaches a terminal state."""
    deadline = time.monotonic() + timeout
    while True:
        job = client.get(f"{API}/certificate-jobs/{job_id}").json()
        if job["status"] in TERMINAL:
            return job
        if time.monotonic() > deadline:
            raise AssertionError(f"job {job_id} did not finish in {timeout}s: {job}")
        time.sleep(0.05)
