"""HTTP handler tests for ``main`` using webtest (WSGI, no async)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from webtest import TestApp

import main


@pytest.fixture
def client(monkeypatch):
    d = MagicMock()
    d.add = MagicMock(return_value={"status": "ok"})
    d.cancel = MagicMock(return_value={"status": "ok"})
    d.clear = MagicMock(return_value={"status": "ok"})
    d.queue = MagicMock()
    d.done = MagicMock()
    d.queue.dict = {}
    d.done.dict = {}
    d.get = MagicMock(return_value=([], []))
    monkeypatch.setattr(main, "dqueue", d)
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
    resp = client.post_json('/add', _valid_video_add_body())
    assert resp.status_int == 200
    assert resp.json['status'] == 'ok'
    main.dqueue.add.assert_called_once()


def test_add_missing_url_returns_400(client):
    resp = client.post_json('/add', {"download_type": "video", "quality": "best", "format": "any"}, expect_errors=True)
    assert resp.status_int == 400
    main.dqueue.add.assert_not_called()


def test_add_invalid_download_type(client):
    resp = client.post_json('/add', _valid_video_add_body(download_type="invalid"), expect_errors=True)
    assert resp.status_int == 400


def test_add_invalid_video_quality(client):
    resp = client.post_json('/add', _valid_video_add_body(quality="9999"), expect_errors=True)
    assert resp.status_int == 400


def test_add_invalid_json_body(client):
    resp = client.post('/add', params='not-json', content_type='application/json', expect_errors=True)
    assert resp.status_int == 400


def test_delete_missing_ids(client):
    resp = client.post_json('/delete', {"where": "queue"}, expect_errors=True)
    assert resp.status_int == 400


def test_delete_queue_calls_cancel(client):
    resp = client.post_json('/delete', {"where": "queue", "ids": ["http://x"]})
    assert resp.status_int == 200
    main.dqueue.cancel.assert_called_once_with(["http://x"])


def test_cookie_status(client):
    resp = client.get('/cookies')
    assert resp.status_int == 200
    data = resp.json
    assert "has_cookies" in data


def test_upload_cookies_missing_field(client):
    resp = client.post('/cookies', expect_errors=True)
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

    client = TestApp(main.app)

    blocked = client.get("/download/.metube/cookies.txt", expect_errors=True)
    assert blocked.status_int == 404

    allowed = client.get("/download/video.mp4")
    assert allowed.status_int == 200
    assert allowed.body == b"video"
