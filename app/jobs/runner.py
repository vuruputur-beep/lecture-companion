"""Durable background job runner.

- Jobs live in SQLite (table `jobs`) so they survive restarts.
- A thread pool with a configurable concurrency limit (default 2) executes them.
- Each handler receives a JobContext with progress/checkpoint/cancel helpers.
- Checkpoints are JSON blobs saved after each completed step; on restart,
  jobs found in `running` state are re-queued and resume from their checkpoint.
- Transient failures retry with backoff up to `max_attempts`.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

from .. import db as dbm
from ..config import load_config

log = logging.getLogger("lc.jobs")


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, job_id: str, lecture_id: str):
        self.job_id = job_id
        self.lecture_id = lecture_id
        self._lock = threading.Lock()

    def _update(self, **fields: Any) -> None:
        with dbm.session() as s:
            job = s.get(dbm.Job, self.job_id)
            if not job:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            s.commit()

    def progress(self, fraction: float, message: str = "") -> None:
        self.check_cancel()
        with dbm.session() as s:
            job = s.get(dbm.Job, self.job_id)
            if job:
                job.progress = max(0.0, min(1.0, fraction))
                if message:
                    job.message = message
                    lg = list(job.log or [])
                    lg.append({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "m": message})
                    job.log = lg[-200:]
                s.commit()
        log.info("[%s] %.0f%% %s", self.job_id, fraction * 100, message)

    def checkpoint(self, data: dict[str, Any]) -> None:
        self._update(checkpoint=data)

    def get_checkpoint(self) -> dict[str, Any]:
        with dbm.session() as s:
            job = s.get(dbm.Job, self.job_id)
            return dict(job.checkpoint or {}) if job else {}

    def add_cost(self, cost_usd: float, tokens_in: int, tokens_out: int) -> None:
        with dbm.session() as s:
            job = s.get(dbm.Job, self.job_id)
            if job:
                job.cost_usd = (job.cost_usd or 0) + cost_usd
                job.tokens_in = (job.tokens_in or 0) + tokens_in
                job.tokens_out = (job.tokens_out or 0) + tokens_out
                lec = s.get(dbm.Lecture, self.lecture_id)
                if lec:
                    lec.cost_usd = (lec.cost_usd or 0) + cost_usd
                    lec.tokens_in = (lec.tokens_in or 0) + tokens_in
                    lec.tokens_out = (lec.tokens_out or 0) + tokens_out
                s.commit()

    def job_cost(self) -> float:
        with dbm.session() as s:
            job = s.get(dbm.Job, self.job_id)
            return float(job.cost_usd or 0) if job else 0.0

    def check_cancel(self) -> None:
        with dbm.session() as s:
            job = s.get(dbm.Job, self.job_id)
            if job and job.cancel_requested:
                raise JobCancelled()


Handler = Callable[[JobContext, dict[str, Any]], None]
_handlers: dict[str, Handler] = {}


def register(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _handlers[kind] = fn
        return fn
    return deco


class JobRunner:
    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self.max_attempts = 3

    # ---- lifecycle ----
    def start(self) -> None:
        cfg = load_config()
        n = max(1, int(cfg.get("max_concurrent_jobs", 2)))
        self._executor = ThreadPoolExecutor(max_workers=n, thread_name_prefix="lcjob")
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="lc-scheduler", daemon=True)
        self._thread.start()
        log.info("Job runner started with %d workers", n)

    def stop(self) -> None:
        self._stop.set()
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def recover(self) -> None:
        """Re-queue jobs that were running when the process last stopped."""
        with dbm.session() as s:
            running = s.scalars(select(dbm.Job).where(dbm.Job.status == "running")).all()
            for job in running:
                job.status = "queued"
                job.message = "Recovered after restart; resuming from last checkpoint."
                lg = list(job.log or [])
                lg.append({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "m": job.message})
                job.log = lg
            s.commit()
            if running:
                log.info("Recovered %d interrupted job(s)", len(running))

    # ---- API ----
    def enqueue(self, lecture_id: str, kind: str, params: dict[str, Any] | None = None) -> str:
        with dbm.session() as s:
            job = dbm.Job(lecture_id=lecture_id, kind=kind, params=params or {}, status="queued")
            s.add(job)
            lec = s.get(dbm.Lecture, lecture_id)
            if lec and kind in ("generate_notes", "regenerate_sections"):
                lec.status = "queued"
            s.commit()
            return job.id

    def cancel(self, job_id: str) -> None:
        with dbm.session() as s:
            job = s.get(dbm.Job, job_id)
            if job and job.status in ("queued", "running"):
                job.cancel_requested = True
                if job.status == "queued":
                    job.status = "cancelled"
                    job.finished_at = dbm.now()
                    lec = s.get(dbm.Lecture, job.lecture_id)
                    if lec and lec.status in ("queued", "processing"):
                        lec.status = "processed" if lec.notes_json else "imported"
                s.commit()

    def retry(self, job_id: str) -> None:
        with dbm.session() as s:
            job = s.get(dbm.Job, job_id)
            if job and job.status in ("failed", "cancelled"):
                job.status = "queued"
                job.cancel_requested = False
                job.error = None
                job.attempts = 0
                s.commit()

    # ---- scheduling loop ----
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            self._stop.wait(1.5)

    def _tick(self) -> None:
        cfg = load_config()
        limit = max(1, int(cfg.get("max_concurrent_jobs", 2)))
        with self._lock:
            free = limit - len(self._active)
        if free <= 0 or not self._executor:
            return
        with dbm.session() as s:
            queued = s.scalars(select(dbm.Job).where(dbm.Job.status == "queued")
                               .order_by(dbm.Job.created_at).limit(free)).all()
            # one job per lecture at a time keeps lecture files isolated
            active_lectures = set()
            with self._lock:
                for jid in self._active:
                    j = s.get(dbm.Job, jid)
                    if j:
                        active_lectures.add(j.lecture_id)
            picked = []
            for job in queued:
                if job.lecture_id in active_lectures:
                    continue
                job.status = "running"
                job.started_at = dbm.now()
                job.attempts = (job.attempts or 0) + 1
                lec = s.get(dbm.Lecture, job.lecture_id)
                if lec and job.kind in ("generate_notes", "regenerate_sections"):
                    lec.status = "processing"
                picked.append((job.id, job.lecture_id, job.kind, dict(job.params or {})))
                active_lectures.add(job.lecture_id)
            s.commit()
        for jid, lid, kind, params in picked:
            with self._lock:
                self._active.add(jid)
            self._executor.submit(self._run, jid, lid, kind, params)

    def _run(self, job_id: str, lecture_id: str, kind: str, params: dict[str, Any]) -> None:
        ctx = JobContext(job_id, lecture_id)
        handler = _handlers.get(kind)
        t0 = time.time()
        try:
            if handler is None:
                raise RuntimeError(f"No handler registered for job kind '{kind}'")
            handler(ctx, params)
            with dbm.session() as s:
                job = s.get(dbm.Job, job_id)
                if job:
                    job.status = "succeeded"
                    job.progress = 1.0
                    job.finished_at = dbm.now()
                    job.message = job.message or "Done"
                    lec = s.get(dbm.Lecture, lecture_id)
                    if lec:
                        lec.processing_seconds = (lec.processing_seconds or 0) + (time.time() - t0)
                        if kind in ("generate_notes", "regenerate_sections"):
                            lec.status = "processed"
                s.commit()
        except JobCancelled:
            with dbm.session() as s:
                job = s.get(dbm.Job, job_id)
                if job:
                    job.status = "cancelled"
                    job.finished_at = dbm.now()
                    job.message = "Cancelled"
                    lec = s.get(dbm.Lecture, lecture_id)
                    if lec:
                        lec.status = "processed" if lec.notes_json else "imported"
                s.commit()
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            log.error("job %s failed: %s\n%s", job_id, err, traceback.format_exc())
            transient = any(w in str(e).lower() for w in ("rate limit", "network", "timeout", "overloaded", "529", "503", "500"))
            with dbm.session() as s:
                job = s.get(dbm.Job, job_id)
                if job:
                    job.error = err
                    if transient and (job.attempts or 0) < self.max_attempts and not job.cancel_requested:
                        job.status = "queued"
                        job.message = f"Transient error, retry {job.attempts}/{self.max_attempts}: {err}"
                    else:
                        job.status = "failed"
                        job.finished_at = dbm.now()
                        job.message = f"Failed: {err}"
                        lec = s.get(dbm.Lecture, lecture_id)
                        if lec:
                            lec.status = "failed" if not lec.notes_json else "processed"
                s.commit()
            if transient:
                time.sleep(min(30, 5 * (2 ** 1)))
        finally:
            with self._lock:
                self._active.discard(job_id)


runner = JobRunner()
