"""In-process background worker pool.

Why in-process (and not Celery) — full reasoning in the README, in short:

* The brief's scale (hundreds–thousands of certificates per job) is easily
  handled by one process: rendering a ReportLab PDF takes ~10–30 ms, so a
  1000-recipient job finishes in well under a minute.
* Zero external infrastructure: reviewers can run ``uvicorn`` and the tests
  with no broker/redis. Operationally simpler = fewer failure modes.
* The HTTP contract is *already* the decoupled one (submit → 202 → poll
  status), so swapping this class for a Celery ``.delay()`` later changes
  ~10 lines, not the API.

Mechanics:
* Jobs are queued on an ``asyncio.Queue``; ``concurrency`` worker coroutines
  consume them.
* The CPU-bound rendering runs via ``asyncio.to_thread`` so the event loop —
  and therefore the status/download endpoints — stays responsive while
  certificates are being generated.
* ``submit()`` is thread-safe (``loop.call_soon_threadsafe``) because route
  handlers may run in FastAPI's threadpool.
* On startup, jobs left ``queued``/``processing`` by a previous crash are
  re-queued automatically (the engine makes reprocessing idempotent).
"""
from __future__ import annotations

import asyncio
import logging

from .engine import SessionFactory, find_resumable_jobs, process_job
from .storage import LocalStorage

logger = logging.getLogger(__name__)


class BackgroundWorker:
    def __init__(self, session_factory: SessionFactory, storage: LocalStorage, concurrency: int = 2):
        self._session_factory = session_factory
        self._storage = storage
        self._concurrency = concurrency
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        for i in range(self._concurrency):
            self._tasks.append(asyncio.create_task(self._run(f"worker-{i}"), name=f"cert-worker-{i}"))

        # Crash recovery: pick up where a previous process left off.
        for job_id in find_resumable_jobs(self._session_factory):
            logger.info("re-queueing interrupted job %s", job_id)
            self.submit(job_id)

    async def stop(self) -> None:
        """Cancel workers. In-flight *threads* finish their current recipient,
        but the job may stay non-terminal — startup recovery re-queues it."""
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    @property
    def running(self) -> bool:
        return bool(self._tasks) and not self._stopping

    # -- job flow ----------------------------------------------------------- #

    def submit(self, job_id: str) -> None:
        """Enqueue a job. Thread-safe: callable from the event loop or from
        any of FastAPI's threadpool threads."""
        if self._loop is None or self._loop.is_closed():
            raise RuntimeError("background worker is not running")
        self._loop.call_soon_threadsafe(self._queue.put_nowait, job_id)

    async def drain(self) -> None:
        """Block until every submitted job has been fully processed.

        Used by tests for deterministic assertions (no sleep-polling).
        """
        await self._queue.join()

    async def _run(self, name: str) -> None:
        logger.debug("%s started", name)
        while not self._stopping:
            job_id = await self._queue.get()
            try:
                await asyncio.to_thread(
                    process_job, self._session_factory, self._storage, job_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # process_job() already isolates every error it can; this
                # guard ensures a truly unexpected failure (e.g. the DB
                # vanished) can never kill the worker pool itself.
                logger.exception("%s: unexpected error processing job %s", name, job_id)
            finally:
                self._queue.task_done()
        logger.debug("%s stopped", name)
