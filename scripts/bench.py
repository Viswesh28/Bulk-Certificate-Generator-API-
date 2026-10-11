"""Rough throughput check: submit one bulk job and time it end-to-end.

    python scripts/bench.py 1000
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def main(n: int, workers: int) -> None:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        database_url=f"sqlite:///{tmp / 'bench.db'}",
        storage_dir=tmp / "store",
        worker_concurrency=workers,
        max_recipients_per_job=max(n, 1000),
    )
    payload = {
        "course_title": "Benchmark Course",
        "issuer_name": "Bench Org",
        "recipients": [{"full_name": f"Participant {i:05d}", "email": f"p{i}@example.com"} for i in range(n)],
    }
    with TestClient(create_app(settings), raise_server_exceptions=True) as client:
        start = time.perf_counter()
        job_id = client.post("/api/v1/certificate-jobs", json=payload).json()["id"]
        accepted = time.perf_counter() - start
        while True:
            job = client.get(f"/api/v1/certificate-jobs/{job_id}").json()
            if job["status"] in ("completed", "completed_with_errors", "failed"):
                break
            time.sleep(0.1)
        total = time.perf_counter() - start
    print(f"recipients={n} workers={workers} status={job['status']} ok={job['succeeded_count']}")
    print(f"accepted (HTTP 202) in {accepted*1000:.0f} ms; finished in {total:.2f} s "
          f"({n/total:.0f} certificates/s)")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    worker_count = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    main(count, worker_count)
