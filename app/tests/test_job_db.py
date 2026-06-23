from __future__ import annotations

from pathlib import Path

from job_db import JobDB
from job_models import JobCreate, JobStatus


def test_create_and_get_job(tmp_path: Path):
    db = JobDB(tmp_path / "jobs.sqlite3")
    created = db.create_job(
        "job-1",
        JobCreate(
            url="https://example.com/watch?v=1",
            download_type="video",
            codec="auto",
            format="any",
            quality="best",
        ),
        "Video",
    )
    assert created.id == "job-1"
    assert created.status == JobStatus.QUEUED
    assert created.created_at is not None
    assert created.updated_at is not None
    assert created.cancel_requested_at is None
    fetched = db.get_job("job-1")
    assert fetched.url == "https://example.com/watch?v=1"
    db.close()


def test_claim_and_finish(tmp_path: Path):
    db = JobDB(tmp_path / "jobs.sqlite3")
    db.create_job(
        "job-2",
        JobCreate(
            url="https://example.com/watch?v=2",
            download_type="video",
            codec="auto",
            format="any",
            quality="best",
        ),
        "Video 2",
    )
    claimed = db.claim_next_queued()
    assert claimed is not None
    assert claimed.status == JobStatus.RUNNING
    db.mark_finished(claimed.id)
    done = db.list_done()
    assert len(done) == 1
    assert done[0].status == JobStatus.FINISHED
    db.close()


def test_request_cancel_queued(tmp_path: Path):
    db = JobDB(tmp_path / "jobs.sqlite3")
    db.create_job(
        "job-3",
        JobCreate(
            url="https://example.com/watch?v=3",
            download_type="audio",
            codec="auto",
            format="mp3",
            quality="best",
        ),
        "Audio 3",
    )
    status = db.request_cancel("job-3")
    assert status == JobStatus.CANCELED
    canceled = db.get_job("job-3")
    assert canceled.status == JobStatus.CANCELED
    assert canceled.cancel_requested_at is not None
    db.close()
