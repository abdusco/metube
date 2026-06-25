from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from job_manager import JobManager
from job_models import EnqueueJobResult, JobCreate, JobList


class _Config:
    def __init__(self, base: Path) -> None:
        self.STATE_DIR = base / "state"
        self.DOWNLOAD_DIR = base / "downloads"
        self.TEMP_DIR = base / "downloads"
        self.MAX_CONCURRENT_DOWNLOADS = 2
        self.CLEAR_COMPLETED_AFTER = 0
        self.YTDL_OPTIONS = {}
        self.OUTPUT_TEMPLATE = "%(title)s.%(ext)s"
        self.DELETE_FILE_ON_TRASHCAN = True


def test_get_queue_shape(tmp_path: Path):
    cfg = _Config(tmp_path)
    cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    manager = JobManager(cfg)
    try:
        data = manager.get_jobs()
        assert isinstance(data, JobList)
        assert data.queued == []
        assert data.done == []
    finally:
        manager.shutdown()


def test_enqueue_creates_queued_job(tmp_path: Path):
    cfg = _Config(tmp_path)
    cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    manager = JobManager(cfg)
    try:
        with patch("job_manager.JobManager._extract_title", return_value="Example"):
            out = manager.enqueue(
                JobCreate(
                    url="https://example.com/watch?v=1",
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    subtitle_langs=[],
                )
            )
        assert isinstance(out, EnqueueJobResult)
        assert out.id
        queue = manager.get_jobs()
        assert len(queue.queued) == 1
        assert queue.queued[0].created_at is not None
    finally:
        manager.shutdown()


def test_logs_default_empty(tmp_path: Path):
    cfg = _Config(tmp_path)
    cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
    cfg.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    manager = JobManager(cfg)
    try:
        assert manager.get_logs("missing") == []
    finally:
        manager.shutdown()
