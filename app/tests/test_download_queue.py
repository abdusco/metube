"""Tests for ``DownloadQueue`` with mocked yt-dlp extraction."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from ytdl import DownloadInfo, DownloadQueue


@pytest.fixture
def dq_env():
    with tempfile.TemporaryDirectory() as tmp:
        dl = os.path.join(tmp, "downloads")
        st = os.path.join(tmp, "state")
        os.makedirs(dl, exist_ok=True)
        os.makedirs(st, exist_ok=True)
        cfg = MagicMock()
        cfg.STATE_DIR = st
        cfg.DOWNLOAD_DIR = dl
        cfg.TEMP_DIR = dl
        cfg.MAX_CONCURRENT_DOWNLOADS = 3
        cfg.YTDL_OPTIONS = {}
        cfg.CLEAR_COMPLETED_AFTER = 0
        cfg.DELETE_FILE_ON_TRASHCAN = False
        cfg.OUTPUT_TEMPLATE = "%(title)s.%(ext)s"
        cfg.OUTPUT_TEMPLATE_PLAYLIST = ""
        cfg.OUTPUT_TEMPLATE_CHANNEL = ""
        yield cfg


def test_get_returns_tuple_of_lists(dq_env):
    notifier = MagicMock()
    dq = DownloadQueue(dq_env, notifier)
    q, done = dq.get()
    assert q == [] and done == []


def test_add_single_video_queued_immediately(dq_env):
    notifier = MagicMock()

    def fake_extract(self, url):
        return {
            "_type": "video",
            "id": "vid1",
            "title": "Test Video",
            "url": url,
            "webpage_url": url,
        }

    dq = DownloadQueue(dq_env, notifier)
    url = "https://example.com/watch?v=1"
    with patch.object(DownloadQueue, "_DownloadQueue__extract_info", fake_extract), \
         patch('ytdl.threading.Thread') as MockThread:
        MockThread.return_value = MagicMock()
        result = dq.add(url, "video", "auto", "any", "best")
    assert result["status"] == "ok"
    assert dq.queue.exists(url)


def test_cancel_removes_from_queue(dq_env):
    notifier = MagicMock()

    def fake_extract(self, url):
        return {
            "_type": "video",
            "id": "vid1",
            "title": "Test Video",
            "url": url,
            "webpage_url": url,
        }

    dq = DownloadQueue(dq_env, notifier)
    url = "https://example.com/cancel-me"
    with patch.object(DownloadQueue, "_DownloadQueue__extract_info", fake_extract), \
         patch('ytdl.threading.Thread') as MockThread:
        MockThread.return_value = MagicMock()
        dq.add(url, "video", "auto", "any", "best")

    dq.cancel([url])
    assert not dq.queue.exists(url)
    notifier.canceled.assert_called()


def test_cancel_before_start_marks_download_canceled(dq_env):
    """Regression: cancel() after add() but before the thread acquires the semaphore
    must flip canceled=True and remove from queue."""
    notifier = MagicMock()

    def fake_extract(self, url):
        return {
            "_type": "video",
            "id": "vid1",
            "title": "Test Video",
            "url": url,
            "webpage_url": url,
        }

    dq = DownloadQueue(dq_env, notifier)
    url = "https://example.com/race"
    with patch.object(DownloadQueue, "_DownloadQueue__extract_info", fake_extract), \
         patch('ytdl.threading.Thread') as MockThread:
        MockThread.return_value = MagicMock()
        dq.add(url, "video", "auto", "any", "best")
        assert dq.queue.exists(url)
        download = dq.queue.get(url)
        assert download.canceled is False
        dq.cancel([url])
        assert not dq.queue.exists(url)
        assert download.canceled is True
        notifier.canceled.assert_called_with(url)


def test_add_entry_queues_single_video_without_reextracting(dq_env):
    notifier = MagicMock()
    dq = DownloadQueue(dq_env, notifier)
    entry = {
        "_type": "video",
        "id": "vid1",
        "title": "Test Video",
        "url": "https://example.com/watch?v=1",
        "webpage_url": "https://example.com/watch?v=1",
        "playlist_index": "01",
        "playlist_title": "Playlist",
    }

    with patch.object(DownloadQueue, "_DownloadQueue__extract_info", side_effect=AssertionError("should not re-extract")), \
         patch('ytdl.threading.Thread') as MockThread:
        MockThread.return_value = MagicMock()
        result = dq.add_entry(entry, "video", "auto", "any", "best")

    assert result["status"] == "ok"
    assert dq.queue.exists("https://example.com/watch?v=1")


def test_download_info_to_public_dict_excludes_server_only_fields():
    info = DownloadInfo(
        id="vid1",
        title="Test Video",
        url="https://example.com/watch?v=1",
        quality="best",
        download_type="video",
        codec="auto",
        format="any",
        error=None,
        entry={"id": "vid1", "huge": "x" * 100000},
    )
    info.subtitle_files = [{"filename": "a.srt", "size": 10}]
    public = info.to_public_dict()
    assert "entry" not in public
    assert public["subtitle_files"] == [{"filename": "a.srt", "size": 10}]
    assert public["url"] == "https://example.com/watch?v=1"
    assert public["title"] == "Test Video"
    assert public["status"] == "pending"
