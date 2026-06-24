"""HTTP handler tests for ``main`` using webtest (WSGI, no async)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from webtest import TestApp

import main
from job_models import JobList


@pytest.fixture
def client(monkeypatch):
    manager = MagicMock()
    manager.enqueue = MagicMock()
    manager.enqueue.return_value.id = "job-1"
    manager.cancel = MagicMock(return_value=None)
    manager.clear = MagicMock(return_value=None)
    manager.get_jobs = MagicMock(return_value=JobList())
    manager.get_logs = MagicMock(return_value=[])
    monkeypatch.setattr(main, "job_manager", manager)
    return TestApp(main.app)


def _valid_video_add_body(**kwargs):
    base = {
        "url": "https://example.com/watch?v=1",
        "download_type": "video",
        "codec": "auto",
        "format": "any",
        "quality": "best",
    }
    base.update(kwargs)
    return base


def test_add_ok(client):
    resp = client.post_json("/jobs", _valid_video_add_body())
    assert resp.status_int == 200
    assert resp.json["status"] == "ok"
    assert resp.json["id"] == "job-1"
    main.job_manager.enqueue.assert_called_once()


def test_add_missing_url_returns_400(client):
    resp = client.post_json("/jobs", {"download_type": "video", "quality": "best", "format": "any"}, expect_errors=True)
    assert resp.status_int == 400
    main.job_manager.enqueue.assert_not_called()


def test_add_invalid_download_type(client):
    resp = client.post_json("/jobs", _valid_video_add_body(download_type="invalid"), expect_errors=True)
    assert resp.status_int == 400


def test_add_invalid_json_body(client):
    resp = client.post("/jobs", params="not-json", content_type="application/json", expect_errors=True)
    assert resp.status_int == 400


def test_cancel_requires_object_body(client):
    resp = client.post("/jobs/job-1", params="{}", content_type="application/json", expect_errors=True)
    assert resp.status_int == 405
    assert resp.json["status"] == "error"
    assert "method" in resp.json["message"].lower()


def test_cancel_calls_manager(client):
    resp = client.delete("/jobs/job-1")
    assert resp.status_int == 204
    assert resp.text == ""
    main.job_manager.cancel.assert_called_once_with("job-1")


def test_api_errors_are_json(client):
    resp = client.post("/jobs", params="not-json", content_type="application/json", expect_errors=True)
    assert resp.status_int == 400
    assert resp.json["status"] == "error"
    assert "invalid json" in resp.json["message"].lower()


def test_clear_calls_manager(client):
    resp = client.post_json("/jobs/clear", {})
    assert resp.status_int == 204
    assert resp.text == ""
    main.job_manager.clear.assert_called_once_with()


def test_jobs_returns_object_shape(client):
    resp = client.get("/jobs")
    assert resp.status_int == 200
    assert isinstance(resp.json, dict)
    assert set(resp.json.keys()) == {"queued", "done"}


def test_cookie_status(client):
    resp = client.get("/cookies")
    assert resp.status_int == 200
    assert "domains" in resp.json
    assert isinstance(resp.json["domains"], list)


def test_upload_cookies_missing_field(client):
    resp = client.post("/cookies", expect_errors=True)
    assert resp.status_int == 400


def test_is_within_state_dir_blocks_state_subtree():
    state_dir = Path(main.config.STATE_DIR).resolve()
    assert main._is_within_state_dir(state_dir)
    assert main._is_within_state_dir(state_dir / "cookies.txt")
    assert main._is_within_state_dir(state_dir / "queue" / "item.json")


def test_is_within_state_dir_allows_sibling_downloads():
    download_dir = os.path.realpath(main.config.DOWNLOAD_DIR)
    assert not main._is_within_state_dir(os.path.join(download_dir, "video.mp4"))
    assert not main._is_within_state_dir("/tmp/unrelated/video.mp4")


def test_download_blocks_state_dir_files(monkeypatch, tmp_path):
    download_dir = tmp_path / "downloads"
    state_dir = download_dir / ".metube"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "cookies.txt").write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    (download_dir / "video.mp4").write_bytes(b"video")

    monkeypatch.setattr(main.config, "DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setattr(main.config, "STATE_DIR", str(state_dir))

    test_client = TestApp(main.app)

    blocked = test_client.get("/download/.metube/cookies.txt", expect_errors=True)
    assert blocked.status_int == 404

    allowed = test_client.get("/download/video.mp4")
    assert allowed.status_int == 200
    assert allowed.body == b"video"
