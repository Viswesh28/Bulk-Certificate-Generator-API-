# Bulk Certificate Generator

A backend API that accepts **one request containing many recipients**, generates a
PDF certificate for each valid recipient from a single built-in template, tracks
progress per job and per recipient, and lets the client download the results
individually or as a ZIP.

Stack: **Python 3.12+, FastAPI, SQLAlchemy 2.0, SQLite (default) / any SQL database,
ReportLab, pytest.**

---

## Contents

1. [Quick start](#1-quick-start)
2. [Running the application](#2-running-the-application)
3. [Running the tests](#3-running-the-tests)
4. [Submitting a certificate generation request](#4-submitting-a-certificate-generation-request)
5. [Tracking progress](#5-tracking-progress)
6. [Retrieving generated certificates](#6-retrieving-generated-certificates)
7. [API reference](#7-api-reference)
8. [Architecture](#8-architecture)
9. [Design decisions](#9-design-decisions)
10. [Known limitations](#10-known-limitations)
11. [Possible improvements](#11-possible-improvements)
12. [Project layout](#12-project-layout)

---

## 1. Quick start

Requirements: Python **3.12 or newer** (tested on 3.13). No external services are needed.

```bash
cd bulk-certificate-generator
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # runtime + test dependencies
cp .env.example .env                  # optional; defaults work out of the box
```

`requirements.txt` holds only runtime dependencies; `requirements-dev.txt` adds
pytest, httpx and pypdf (used by the tests).

Configuration (environment variables or `.env`, see `.env.example`):

| Variable               | Default                          | Meaning                                                   |
|------------------------|----------------------------------|-----------------------------------------------------------|
| `DATABASE_URL`         | `sqlite:///./certificates.db`    | Any SQLAlchemy URL. Switch to PostgreSQL for production.  |
| `STORAGE_DIR`          | `storage/certificates`           | Where generated PDFs are written.                         |
| `WORKER_CONCURRENCY`   | `2`                              | Number of jobs processed in parallel (1–16).              |
| `MAX_RECIPIENTS_PER_JOB` | `1000`                         | Larger lists must be split across several jobs.           |

## 2. Running the application

```bash
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

- **Register UI:** <http://localhost:8000/>. A single-page front end for issuing
  batches, watching progress, and downloading results. It lives in
  `app/static/index.html` and has no build step.
- Interactive API docs: <http://localhost:8000/docs>
- Health check: `GET /health`

The app is created with a factory (`create_app`) rather than a module-level
`app`, so importing the module has no side effects (no database file, no worker).
The tables are created automatically on startup.

Unfinished jobs from a previous run (status `queued` or `processing`) are
re-queued automatically on startup.

### Using the register UI

1. Fill in the course, the issuing organisation, and optionally the date and signatory.
2. Enter recipients one per line (`Full name, email`), or load a CSV. *Fill sample* loads a
   short example that includes one duplicate, so you can see a rejection.
3. Submit. The batch opens in the register, where the ledger updates while the job runs.
   Each row shows whether it was issued or rejected, with the reason.
4. Open a finished certificate from its *PDF* link, or use *Download all as ZIP*.

The batch is recorded in the URL (`#job=<id>`), so a refresh or a shared link reopens
it. Recent batches are listed at the bottom of the page.

## 3. Running the tests

```bash
pytest
```

Expected output: `66 passed`. The suite runs in about 3 seconds and needs no
server; each test uses its own temporary SQLite database and storage directory.

What is covered:

| Requirement from the brief | Where it is tested |
|---|---|
| Creating a generation job | `tests/test_api_jobs.py` (202 + `Location`, defaults, persistence) |
| Input validation | `tests/test_api_jobs.py` (422 cases), `tests/test_units.py` (schemas, per-recipient rules) |
| Certificate generation | `tests/test_units.py` (renderer output, long names, missing fields), `tests/test_engine.py` |
| Job status / progress | `tests/test_api_jobs.py` (progress, pagination, filters), `tests/test_engine.py` (counters) |
| Individual certificate failure | `tests/test_engine.py` (render crash, storage crash, duplicate, failed row not poisoning duplicates) and `tests/test_api_jobs.py` (409 for a failed row) |
| Retrieving certificates | `tests/test_api_jobs.py` (single PDF, ZIP, wrong-job 404, 409 while running) |
| Crash recovery / idempotency | `tests/test_engine.py` (resume, no re-rendering, stuck `processing` rows) |

## 4. Submitting a certificate generation request

### With curl

```bash
curl -X POST http://localhost:8000/api/v1/certificate-jobs \
  -H 'Content-Type: application/json' \
  -d @examples/job.json
```

`examples/job.json` looks like this:

```json
{
  "course_title": "Advanced Python Workshop",
  "issuer_name": "Acme Training Corp",
  "issue_date": "2026-10-01",
  "signatory_name": "Jane Doe",
  "signatory_title": "Programme Director",
  "recipients": [
    {"full_name": "Alice Smith", "email": "alice@example.com"},
    {"full_name": "Bob Jones",   "email": "bob@example.com"},
    {"full_name": "Carol White"}
  ]
}
```

Request fields:

| Field | Required | Notes |
|---|---|---|
| `course_title` | yes | 1–200 chars, printed on every certificate |
| `issuer_name` | yes | 1–200 chars, shown as the letterhead |
| `issue_date` | no | `YYYY-MM-DD`; defaults to today (UTC); at most one year ahead |
| `signatory_name`, `signatory_title` | no | Printed under the signature line |
| `recipients` | yes | 1 to `MAX_RECIPIENTS_PER_JOB` items |
| `recipients[].full_name` | yes | 1–200 chars; surrounding and repeated spaces are collapsed |
| `recipients[].email` | no | Must be a valid email address if given |

The response is **`202 Accepted`** immediately, with the job in status `queued`
and a `Location` header pointing to the job resource.

### With the CSV helper

```bash
python scripts/submit_csv.py examples/recipients.csv \
  --course "Advanced Python Workshop" --issuer "Acme Training Corp" \
  --date 2026-10-01 --signatory "Jane Doe" --signatory-title "Programme Director" \
  --base-url http://localhost:8000
```

The CSV needs a `full_name` column and may have an `email` column. The helper
submits the job, polls until it finishes, prints each failed row with its reason,
and saves the ZIP to `downloads/`. It exits with `0` if everything succeeded,
`2` if some rows failed, and `1` if the request was rejected.

## 5. Tracking progress

```bash
curl http://localhost:8000/api/v1/certificate-jobs/<job_id>
```

```json
{
  "id": "33ef4627-…",
  "status": "completed_with_errors",
  "total_recipients": 5,
  "succeeded_count": 3,
  "failed_count": 2,
  "progress_pct": 100.0,
  "error": null,
  "created_at": "2026-10-11T04:14:50",
  "updated_at": "2026-10-11T04:14:50"
}
```

**Job statuses**

| Status | Meaning |
|---|---|
| `queued` | Accepted, waiting for a worker |
| `processing` | A worker is rendering certificates; `progress_pct` moves upward |
| `completed` | Every recipient succeeded |
| `completed_with_errors` | Some recipients succeeded and some failed; the successful certificates are still downloadable |
| `failed` | Every recipient failed, or the job hit a fatal error (see `error`) |

`progress_pct` is `(succeeded + failed) / total × 100`, so it reaches 100 when
processing finishes, whatever the outcome.

**Per-recipient results**

```bash
# all recipients, in submission order
curl "http://localhost:8000/api/v1/certificate-jobs/<job_id>/recipients?page=1&page_size=50"

# only the failures, with reasons
curl "http://localhost:8000/api/v1/certificate-jobs/<job_id>/recipients?status=failed"
```

Each item has `status` (`pending`, `processing`, `generated`, `failed`),
`error_message` for failures, `certificate_uid` and `certificate_url` for successes.

## 6. Retrieving generated certificates

**One certificate (PDF)**

```bash
curl -o certificate.pdf \
  "http://localhost:8000/api/v1/certificate-jobs/<job_id>/recipients/<recipient_id>/certificate"
```

Use the `certificate_url` field from the recipients listing; it already contains the full URL.
Returns `409` if that recipient failed or is still pending.

**All certificates for a job (ZIP)**

```bash
curl -o certificates.zip \
  "http://localhost:8000/api/v1/certificate-jobs/<job_id>/certificates.zip"
```

Returns `409` while the job is still `queued`/`processing`, or if it has no
successful certificates.

Each certificate shows a unique ID (`CERT-XXXXXXXXXXXX`), which is also the
`certificate_uid` stored in the database. The ID is printed on the PDF so a
certificate can be matched back to its database record.

## 7. API reference

Base path: `/api/v1`. Full schemas are in `/docs`.

| Method | Path | Purpose | Success |
|---|---|---|---|
| `POST` | `/certificate-jobs` | Submit a bulk job | `202` |
| `GET` | `/certificate-jobs` | List jobs, newest first (`page`, `page_size`) | `200` |
| `GET` | `/certificate-jobs/{job_id}` | Job status and counters | `200` |
| `GET` | `/certificate-jobs/{job_id}/recipients` | Per-recipient results (`status`, `page`, `page_size`) | `200` |
| `GET` | `/certificate-jobs/{job_id}/recipients/{recipient_id}/certificate` | One PDF | `200` |
| `GET` | `/certificate-jobs/{job_id}/certificates.zip` | All generated PDFs | `200` |
| `GET` | `/health` | Liveness | `200` |

Error responses:

| Code | When |
|---|---|
| `422` | Request body invalid (whole request rejected), or too many recipients |
| `404` | Unknown job or recipient, or a recipient that belongs to another job |
| `409` | Certificate not available yet or failed; ZIP requested before the job finished |

## 8. Architecture

```
 client ──POST /certificate-jobs──▶ FastAPI route
                                      │  validate (Pydantic)
                                      │  one DB transaction: job + all recipients
                                      ▼
                              BackgroundWorker.submit(job_id)  ── returns 202 ──▶ client
                                      │
                    asyncio.Queue ◀───┘
                        │
              N worker coroutines (N = WORKER_CONCURRENCY)
                        │  asyncio.to_thread(...)   ← CPU-bound rendering off the event loop
                        ▼
                 engine.process_job(job_id)
                   for each recipient (in its own try/except):
                     validate → render PDF (ReportLab) → save to storage → mark generated
                     on error → mark failed with reason, continue
                   recompute job counters, set terminal status
                        │
          ┌─────────────┴──────────────┐
          ▼                            ▼
   relational DB                LocalStorage (PDF files)
   jobs + recipients            keyed "<job_id>/<recipient_id>.pdf"

 client ──GET …/recipients, GET …/certificate──▶ reads DB and storage
```

The engine itself has **no FastAPI imports**. It receives a session factory and a
storage object, so it can run inside the worker, inside a test, or inside a
future Celery task without changes.

Diagrams: `docs/architecture.svg` (components) and `docs/flow.md` (Mermaid
sequence and job-state diagrams).

## 9. Design decisions

### 9.1 Framework: FastAPI
Pydantic gives field-level validation with error locations (e.g.
`recipients[1].email`) with almost no code, and the OpenAPI docs are generated
from the same models. Django/DRF would work too but would bring more machinery
than this service needs.

### 9.2 Asynchronous processing, not in the request
Rendering a PDF takes milliseconds, but a 1,000-recipient job takes seconds, and
the brief asks for bulk requests. Generating inside the request would block the
client and tie up a connection. So the API:

1. stores the job and all recipients in **one transaction** (the worker never sees a half-written job),
2. returns **`202 Accepted`** with a job ID,
3. lets the client poll `GET /certificate-jobs/{id}`.

**Why an in-process worker instead of Celery + Redis?** The brief's scale is
handled comfortably by one process, and the in-process worker needs no extra
services, so the project runs with `uvicorn` alone. The HTTP contract
(submit → 202 → poll) is already the one a queue would need, so moving to Celery
later means replacing `BackgroundWorker.submit` and the body of its loop. The
reasoning and migration path are in §11.

Measured on this sandbox (in-process, 2 workers, TestClient):

| Recipients | Time to HTTP 202 | Time to completion | Throughput |
|---|---|---|---|
| 1,000 | ~250 ms | ~4.6 s | ~216 certificates/s |

Reproduce with `python scripts/bench.py 1000 2`. Numbers depend on the machine.

**Concurrency caveat.** ReportLab is pure Python, so the GIL limits how much
multiple threads speed up rendering. The worker pool exists to keep the event
loop responsive and to run several jobs interleaved, not to multiply CPU
throughput. Scaling CPU throughput means running more processes or machines,
which is what a Celery deployment provides (§11).

### 9.3 Two-tier validation
- **Tier 1 — request validation (Pydantic, at the API boundary).** Types, lengths,
  email format, control characters, shared text that the font cannot draw, and
  issue date range. A broken request is rejected **as a whole** with `422`, and
  the error points to the exact field and row. This catches buggy clients early.
- **Tier 2 — per-recipient validation (during processing).** Rules that depend on
  the rest of the batch or on rendering: duplicate `(name, email)` pairs within
  the job, names that the font cannot render, names too short after trimming.
  These fail **only that recipient**.

Trade-off: a single malformed row (say, a bad email) rejects the whole request,
so the client fixes it and resubmits. The alternative is to accept the job with
the bad row marked `failed` up front. That is a reasonable choice too, and it's
a small change in `create_job` if the product needs it.

### 9.4 Failure isolation
- Each recipient is processed in its own `try/except`. A render error, a storage
  error or a validation error marks that recipient `failed` with the reason and
  moves on. Tests cover a render crash, a storage crash, a duplicate and an
  unrenderable name.
- A recipient that failed is rolled back cleanly: no `file_key` or
  `certificate_uid` is left pointing at a missing file.
- A failed row does not block later valid rows. For example, if the first
  "Alice Smith" fails to render, the second "Alice Smith" is still allowed to
  succeed (tested).
- The job's counters are **recomputed from the recipients table** at the end,
  not incremented along the way. The totals are therefore correct even if the
  process crashes in the middle of a job.
- `process_job` never raises. A job-level fatal error marks the job `failed`
  with the reason in `error`, and the worker pool keeps running.

### 9.5 Job status model
Five states (`queued`, `processing`, `completed`, `completed_with_errors`,
`failed`). The difference between `completed_with_errors` and `failed` lets a
client tell "some certificates are ready" from "nothing happened". Each
recipient has its own status and reason, so the failures can be identified and
handled without guessing.

### 9.6 Resilience and idempotency
- **Crash recovery.** On startup, jobs left `queued` or `processing` are
  re-queued. The engine skips recipients that are already `generated`, so
  resumed work does not produce duplicate files, and it resets recipients stuck
  in `processing` to `pending`.
- **Terminal jobs are no-ops.** Re-running a finished job changes nothing.
- **Storage keys are unique per recipient** (`<job_id>/<recipient_id>.pdf`), so
  retries overwrite nothing that belongs to someone else.

### 9.7 Storage
- Files are addressed by **relative keys**. The database holds the key, not an
  absolute path. A future `S3Storage` with the same four methods would replace
  `LocalStorage` without touching the models or the engine.
- `LocalStorage` rejects absolute keys and `..` traversal.

### 9.8 Database
- SQLAlchemy 2.0 ORM, so SQLite (zero setup, used by default and in tests) and
  PostgreSQL work from the same code.
- SQLite runs in **WAL mode** with a busy timeout, so status polling does not
  block the worker writing progress.
- Timestamps are stored as naive UTC and returned as ISO 8601.
- Schema is created with `create_all` on startup. This is a deliberate shortcut
  for the exercise; §11 covers Alembic.

### 9.9 Certificate rendering
- One template, drawn with **ReportLab** vector primitives, landscape A4. No
  template files or image assets are needed, and the output is a small PDF.
- The template uses only PDF **base-14 fonts** (Helvetica, Times, Courier). They
  need no font files, but they can only draw characters in WinAnsi/cp1252 (Latin
  letters, accents, common punctuation). Names outside that set are rejected
  per recipient with a clear message. §10 explains why and how to fix it.
- Long names and course titles are **auto-fitted**, so they shrink to stay
  inside the border.
- The renderer is a **pure function** `CertificateData -> bytes`, which keeps it
  easy to test.

### 9.10 Certificate IDs
Each certificate gets a random ID (`CERT-` + 12 hex characters), printed on the
PDF and stored on the recipient. It makes a certificate traceable to its record,
and it is the natural key for a future public verification endpoint. It is random
rather than sequential, so the IDs do not reveal how many certificates exist.

### 9.11 Retrieval
- Single PDFs are streamed with `FileResponse`, with a readable filename.
- The ZIP endpoint is built in memory. At the default cap of 1,000 recipients
  (roughly 4–6 KB per certificate) that is a few MB, which is fine. Very large
  jobs would stream the archive instead (§11).

## 10. Known limitations

- **Character set.** Names and titles must be encodable in cp1252. Names
  containing, for example, Chinese, Devanagari, Cyrillic or Arabic characters are
  rejected per recipient (with a message). Fix: register a Unicode TTF font such
  as Noto Sans with ReportLab (`pdfmetrics.registerFont(TTFont(...))`) and use it
  in `certificate.py`. The validation rule would then follow the font's glyph
  coverage.
- **Single process.** The worker lives in the API process. Running several API
  replicas would process jobs in each of them, which is fine for correctness
  (the engine is idempotent and each job is claimed by one worker), but it does
  not scale CPU beyond one machine's processes. See §11.
- **No authentication or rate limiting.** Any caller can submit jobs and download
  certificates. A real deployment would put this behind an authenticated gateway
  and scope jobs to an organisation.
- **Download by ID.** Anyone who knows a job or recipient ID can download its
  certificates. IDs are UUIDs/random, but they are not access control.
- **No cancellation.** Once submitted, a job runs to completion.
- **Local disk storage.** Files are not replicated, and the local directory must
  be shared if the API runs on several machines.
- **Tested on SQLite only.** The code is written against SQLAlchemy so it should
  run on PostgreSQL, but the test suite only exercises SQLite. Failure paths are
  injected by monkeypatching the renderer and storage.

## 11. Possible improvements

Roughly in priority order:

1. **Move the worker to a real queue** (Celery or RQ with Redis, or a DB-backed
   queue such as `SELECT … FOR UPDATE SKIP LOCKED` on the jobs table). Then rendering
   runs in separate worker processes or machines, and the API stops competing with
   them for CPU. The engine needs no change, because `process_job(session_factory,
   storage, job_id)` is already a self-contained task.
2. **Unicode fonts** (see §10), with a font registry so the template can change
   font per locale.
3. **Alembic migrations** instead of `create_all`.
4. **Object storage** (S3/GCS/Azure Blob) behind the same storage interface, with
   pre-signed download URLs instead of proxying bytes through the API.
5. **Stream the ZIP** and compress on the fly for very large jobs.
6. **Per-row results at submission time**: accept rows with bad data, mark them
   `failed` immediately, and process the rest (see the trade-off in §9.3).
7. **Auth and multi-tenancy**: API keys, organisation scoping, and rate limits.
8. **Webhooks** when a job finishes, so clients do not need to poll.
9. **Public verification endpoint** `GET /verify/{certificate_uid}`, which the
   ID on each certificate already supports.
10. **Metrics and tracing** (job duration, render time, failure rate) and a
    Dockerfile + `docker-compose.yml` with PostgreSQL.

## 12. Project layout

```
bulk-certificate-generator/
├── app/
│   ├── static/index.html       # the register UI (single file, no build step)
│   ├── main.py                 # create_app(): wires DB, storage, worker, routes, CORS
│   ├── config.py               # Settings (env / .env)
│   ├── database.py             # engine, session factory, SQLite pragmas
│   ├── models.py               # CertificateJob, Recipient, status enums
│   ├── schemas.py              # Pydantic request/response models + tier-1 validation
│   ├── util.py                 # utcnow(), slugify()
│   ├── api/
│   │   ├── deps.py             # FastAPI dependencies (session, storage, worker)
│   │   └── routes.py           # HTTP endpoints
│   └── services/
│       ├── jobs.py             # DB operations for the API (create, list, query)
│       ├── engine.py           # process_job(): per-recipient loop, isolation, counters
│       ├── validation.py       # tier-2, per-recipient rules and duplicate detection
│       ├── certificate.py      # the single ReportLab template
│       ├── storage.py          # LocalStorage with traversal protection
│       └── worker.py           # in-process asyncio worker pool
├── tests/
│   ├── conftest.py             # per-test app, temp DB, polling helper
│   ├── test_api_jobs.py        # HTTP behaviour end to end
│   ├── test_engine.py          # processing rules, isolation, resume
│   └── test_units.py           # validation, schemas, renderer, storage
├── scripts/
│   ├── submit_csv.py           # CLI: CSV → job → wait → ZIP
│   └── bench.py                # throughput check
├── docs/
│   ├── architecture.svg        # component diagram
│   └── flow.md                 # sequence + job-state diagrams (Mermaid)
├── examples/
│   ├── job.json
│   ├── recipients.csv
│   └── sample-output/          # sample certificates generated by the app
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
└── .env.example
```
