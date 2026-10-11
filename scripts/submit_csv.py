"""Command-line client: submit a CSV, wait for the job, download the results.

    python scripts/submit_csv.py examples/recipients.csv \
        --course "Advanced Python Workshop" --issuer "Acme Training Corp" \
        --date 2026-10-01 --base-url http://localhost:8000

Uses only the standard library so it runs without installing anything.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TERMINAL = {"completed", "completed_with_errors", "failed"}


def _request(method: str, url: str, body: dict | None = None) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def read_recipients(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if "full_name" not in (reader.fieldnames or []):
            raise SystemExit("CSV must have a 'full_name' column (optional: 'email')")
        rows = []
        for row in reader:
            entry = {"full_name": row["full_name"]}
            if row.get("email", "").strip():
                entry["email"] = row["email"].strip()
            rows.append(entry)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--course", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--date", dest="issue_date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--signatory", dest="signatory_name")
    parser.add_argument("--signatory-title", dest="signatory_title")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--out", type=Path, default=Path("downloads"), help="where to save the ZIP")
    args = parser.parse_args()

    api = args.base_url.rstrip("/") + "/api/v1"
    payload = {
        "course_title": args.course,
        "issuer_name": args.issuer,
        "issue_date": args.issue_date,
        "signatory_name": args.signatory_name,
        "signatory_title": args.signatory_title,
        "recipients": read_recipients(args.csv_path),
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    status, body = _request("POST", f"{api}/certificate-jobs", payload)
    if status != 202:
        print(f"submission rejected ({status}): {body.decode()}", file=sys.stderr)
        return 1
    job_id = json.loads(body)["id"]
    print(f"job {job_id} accepted for {len(payload['recipients'])} recipients")

    while True:
        _, body = _request("GET", f"{api}/certificate-jobs/{job_id}")
        job = json.loads(body)
        print(f"  {job['status']:<12} {job['progress_pct']:>5.1f}%", end="\r")
        if job["status"] in TERMINAL:
            print()
            break
        time.sleep(1)

    print(f"finished: {job['status']} ({job['succeeded_count']} ok, {job['failed_count']} failed)")
    _, body = _request("GET", f"{api}/certificate-jobs/{job_id}/recipients?status=failed")
    for item in json.loads(body)["items"]:
        print(f"  FAILED row {item['position'] + 1}: {item['full_name']!r} - {item['error_message']}")

    if job["succeeded_count"]:
        status, zip_bytes = _request("GET", f"{api}/certificate-jobs/{job_id}/certificates.zip")
        args.out.mkdir(parents=True, exist_ok=True)
        target = args.out / f"certificates-{job_id}.zip"
        target.write_bytes(zip_bytes)
        print(f"saved {target} ({len(zip_bytes)} bytes)")
    return 0 if job["failed_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
