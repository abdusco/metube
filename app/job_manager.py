from __future__ import annotations

import logging
import threading
import time
import typing
import uuid
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yt_dlp
import yt_dlp.networking.impersonate

from cookies_db import CookieDB
from job_db import JobDB
from logs_db import LogsDB
from job_models import EnqueueJobResult, JobCreate, JobList, JobStatus
from job_worker import run_job

if typing.TYPE_CHECKING:
    from main import Config

log = logging.getLogger("job_manager")


class JobManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        state_dir = Path(self.config.STATE_DIR)
        self.db = JobDB(state_dir / "jobs.sqlite3")
        self.cookies_db = CookieDB(state_dir / "jobs.sqlite3")
        self.logs_db = LogsDB(state_dir / "jobs.sqlite3")
        self.db.reset_running_jobs_to_error()
        self._executor = ThreadPoolExecutor(max_workers=int(self.config.MAX_CONCURRENT_DOWNLOADS))
        self._active_futures: dict[str, Future] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._logs: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=2000))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._scheduler = threading.Thread(target=self._scheduler_loop, daemon=True, name="job-scheduler")
        self._scheduler.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._scheduler.join(timeout=2)
        with self._lock:
            for event in self._cancel_events.values():
                event.set()
        self._executor.shutdown(wait=False)
        self.db.close()
        self.cookies_db.close()
        self.logs_db.close()

    def _extract_title(self, url: str) -> str:
        params = {
            **dict(self.config.YTDL_OPTIONS),
            "quiet": True,
            "verbose": False,
            "no_color": True,
            "extract_flat": True,
            "ignore_no_formats_error": True,
            "noplaylist": True,
            "paths": {"home": self.config.DOWNLOAD_DIR, "temp": self.config.TEMP_DIR},
        }
        imp = params.get("impersonate")
        if imp is not None:
            params["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(imp)
        info = yt_dlp.YoutubeDL(params=params).extract_info(url, download=False)
        if isinstance(info, dict):
            return str(info.get("title") or info.get("id") or url)
        return url

    def enqueue(self, job_create: JobCreate) -> EnqueueJobResult:
        self._clear_expired()
        title = self._extract_title(job_create.url)
        job_id = str(uuid.uuid4())
        self.db.create_job(job_id, job_create, title)
        return EnqueueJobResult(id=job_id)

    def _clear_expired(self) -> None:
        if not self.config.CLEAR_COMPLETED_AFTER:
            return
        threshold = datetime.now(UTC) - timedelta(seconds=self.config.CLEAR_COMPLETED_AFTER)
        jobs = self.db.list_done_older_than(threshold)
        if not jobs:
            return
        if self.config.DELETE_FILE_ON_TRASHCAN:
            base = Path(self.config.DOWNLOAD_DIR)
            for job in jobs:
                rel_names = [job.filename] if job.filename else []
                rel_names += [sf.filename for sf in job.subtitle_files if sf.filename]
                for rel_name in rel_names:
                    try:
                        (base / rel_name).unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        log.warning("Could not delete file %s for %s: %s", rel_name, job.id, exc)
        ids = [job.id for job in jobs]
        for job_id in ids:
            self._logs.pop(job_id, None)
        self.db.delete_done(ids)

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            self._cleanup_finished_futures()
            available = int(self.config.MAX_CONCURRENT_DOWNLOADS) - self._running_count()
            if available <= 0:
                time.sleep(0.2)
                continue
            claimed = 0
            for _ in range(available):
                job = self.db.claim_next_queued()
                if job is None:
                    break
                cancel_event = threading.Event()
                with self._lock:
                    self._cancel_events[job.id] = cancel_event
                future = self._executor.submit(self._run_single_job, job.id, cancel_event)
                with self._lock:
                    self._active_futures[job.id] = future
                claimed += 1
            if claimed == 0:
                time.sleep(0.2)

    def _running_count(self) -> int:
        with self._lock:
            return sum(1 for fut in self._active_futures.values() if not fut.done())

    def _cleanup_finished_futures(self) -> None:
        with self._lock:
            finished = [job_id for job_id, fut in self._active_futures.items() if fut.done()]
            for job_id in finished:
                self._active_futures.pop(job_id, None)
                self._cancel_events.pop(job_id, None)

    def _run_single_job(self, job_id: str, cancel_event: threading.Event) -> None:
        job = self.db.get_job(job_id)
        try:
            run_job(
                self.db,
                job,
                download_dir=Path(self.config.DOWNLOAD_DIR),
                temp_dir=Path(self.config.TEMP_DIR),
                output_template=self.config.OUTPUT_TEMPLATE,
                ytdl_options=dict(self.config.YTDL_OPTIONS),
                cancel_event=cancel_event,
                log_line=lambda line: self._logs[job_id].append(line),
                cookies_content=self.cookies_db.get_merged_content() or None,
            )
        finally:
            lines = list(self._logs.get(job_id, []))
            if lines:
                self.logs_db.save(job_id, lines)

    def list_cookie_domains(self) -> list[str]:
        return self.cookies_db.list_domains()

    def upsert_cookies_for_domain(self, domain: str, content: str) -> None:
        self.cookies_db.upsert(domain, content)

    def delete_cookies_for_domain(self, domain: str) -> None:
        self.cookies_db.delete_domain(domain)

    def delete_all_cookies(self) -> None:
        self.cookies_db.delete_all()

    def retry(self, job_id: str) -> EnqueueJobResult:
        self._clear_expired()
        job = self.db.get_job(job_id)
        if job.status not in (JobStatus.FINISHED, JobStatus.ERROR, JobStatus.CANCELED):
            raise ValueError(f"job {job_id} is not in a done state")
        self.db.delete_done([job_id])
        self._logs.pop(job_id, None)
        new_id = str(uuid.uuid4())
        job_create = JobCreate(
            url=job.url,
            download_type=job.download_type,
            codec=job.codec,
            format=job.format,
            quality=job.quality,
            subtitle_langs=job.subtitle_langs,
        )
        self.db.create_job(new_id, job_create, job.title)
        return EnqueueJobResult(id=new_id)

    def delete_job(self, job_id: str) -> None:
        status = self.db.request_cancel(job_id)
        if status == JobStatus.RUNNING:
            with self._lock:
                event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()
            return
        try:
            job = self.db.get_job(job_id)
        except KeyError:
            return
        if self.config.DELETE_FILE_ON_TRASHCAN:
            base = Path(self.config.DOWNLOAD_DIR)
            for rel_name in job.files:
                try:
                    (base / rel_name).unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    log.warning("Could not delete file %s for %s: %s", rel_name, job_id, exc)
        self._logs.pop(job_id, None)
        self.db.delete_done([job_id])

    def clear(self) -> None:
        for job_id in self.db.list_done_ids():
            self.delete_job(job_id)

    def get_jobs(self) -> JobList:
        return JobList(queued=self.db.list_queued(), done=self.db.list_done())

    def get_logs(self, job_id: str) -> list[str]:
        if job_id in self._logs:
            return list(self._logs[job_id])
        return self.logs_db.get_lines(job_id)
