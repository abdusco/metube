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
        cfg.AUDIO_DOWNLOAD_DIR = dl
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

    def fake_extract(self, url, ytdl_options_overrides=None):
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
        result = dq.add(url, "video", "auto", "any", "best", "")
    assert result["status"] == "ok"
    assert dq.queue.exists(url)


def test_cancel_removes_from_queue(dq_env):
    notifier = MagicMock()

    def fake_extract(self, url, ytdl_options_overrides=None):
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
        dq.add(url, "video", "auto", "any", "best", "")

    dq.cancel([url])
    assert not dq.queue.exists(url)
    notifier.canceled.assert_called()


def test_cancel_before_start_marks_download_canceled(dq_env):
    """Regression: cancel() after add() but before the thread acquires the semaphore
    must flip canceled=True and remove from queue."""
    notifier = MagicMock()

    def fake_extract(self, url, ytdl_options_overrides=None):
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
        dq.add(url, "video", "auto", "any", "best", "")
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
        result = dq.add_entry(entry, "video", "auto", "any", "best", "")

    assert result["status"] == "ok"
    assert dq.queue.exists("https://example.com/watch?v=1")


def test_add_merges_global_and_override_options(dq_env):
    notifier = MagicMock()
    dq_env.YTDL_OPTIONS = {"writesubtitles": False, "cookiefile": "/tmp/global.txt"}

    def fake_extract(self, url, ytdl_options_overrides=None):
        return {
            "_type": "video",
            "id": "vid2",
            "title": "Preset Video",
            "url": url,
            "webpage_url": url,
        }

    dq = DownloadQueue(dq_env, notifier)
    url = "https://example.com/preset"
    with patch.object(DownloadQueue, "_DownloadQueue__extract_info", fake_extract), \
         patch('ytdl.threading.Thread') as MockThread:
        MockThread.return_value = MagicMock()
        result = dq.add(
            url, "video", "auto", "any", "best", "",
            ytdl_options_overrides={"proxy": "http://override", "embed_thumbnail": True, "ratelimit": 1000},
        )

    assert result["status"] == "ok"
    queued = dq.queue.get(url)
    assert queued.ytdl_opts["cookiefile"] == "/tmp/global.txt"
    assert queued.ytdl_opts["ratelimit"] == 1000
    assert queued.ytdl_opts["proxy"] == "http://override"
    assert queued.ytdl_opts["embed_thumbnail"] is True


def test_extract_info_override_null_download_archive_overrides_global(dq_env):
    dq_env.YTDL_OPTIONS = {"download_archive": "/tmp/archive.txt"}

    captured_params: list = []

    class FakeYoutubeDL:
        def __init__(self, params=None):
            captured_params.append(params)

        def extract_info(self, url, download=False):
            return {
                "_type": "video",
                "id": "vid-archive",
                "title": "Archive Test",
                "url": url,
                "webpage_url": url,
            }

    notifier = MagicMock()
    dq = DownloadQueue(dq_env, notifier)
    with patch("ytdl.yt_dlp.YoutubeDL", FakeYoutubeDL), \
         patch('ytdl.threading.Thread') as MockThread:
        MockThread.return_value = MagicMock()
        result = dq.add(
            "https://example.com/archive-test", "video", "auto", "any", "best", "",
            ytdl_options_overrides={"download_archive": None},
        )

    assert result["status"] == "ok"
    assert len(captured_params) == 1
    extract_params = captured_params[0]
    assert extract_params.get("download_archive") is None
    assert extract_params["extract_flat"] is True
    assert extract_params["noplaylist"] is True


def test_extract_info_metube_extract_keys_win_over_overrides(dq_env):
    """MeTube's flat-extract settings must not be overridden by overrides."""
    dq_env.YTDL_OPTIONS = {}

    captured_params: list = []

    class FakeYoutubeDL:
        def __init__(self, params=None):
            captured_params.append(params)

        def extract_info(self, url, download=False):
            return {
                "_type": "video",
                "id": "vid-flat",
                "title": "Flat Test",
                "url": url,
                "webpage_url": url,
            }

    notifier = MagicMock()
    dq = DownloadQueue(dq_env, notifier)
    with patch("ytdl.yt_dlp.YoutubeDL", FakeYoutubeDL), \
         patch('ytdl.threading.Thread') as MockThread:
        MockThread.return_value = MagicMock()
        result = dq.add(
            "https://example.com/flat-test", "video", "auto", "any", "best", "",
            ytdl_options_overrides={"extract_flat": False, "noplaylist": False},
        )

    assert result["status"] == "ok"
    assert captured_params[0]["extract_flat"] is True
    assert captured_params[0]["noplaylist"] is True


def test_calc_download_path_allows_subfolder(dq_env):
    notifier = MagicMock()
    dq = DownloadQueue(dq_env, notifier)
    path, err = dq._DownloadQueue__calc_download_path("video", "sub/dir")
    assert err is None
    assert os.path.realpath(path) == os.path.join(os.path.realpath(dq_env.DOWNLOAD_DIR), "sub", "dir")


def test_calc_download_path_rejects_sibling_prefix_escape(dq_env):
    notifier = MagicMock()
    base = os.path.realpath(dq_env.DOWNLOAD_DIR)
    sibling = base + "-secret"
    os.makedirs(sibling, exist_ok=True)
    dq = DownloadQueue(dq_env, notifier)
    escape_folder = os.path.join("..", os.path.basename(sibling), "x")
    path, err = dq._DownloadQueue__calc_download_path("video", escape_folder)
    assert path is None
    assert err is not None and err["status"] == "error"


def test_calc_download_path_rejects_parent_escape(dq_env):
    notifier = MagicMock()
    dq = DownloadQueue(dq_env, notifier)
    path, err = dq._DownloadQueue__calc_download_path("video", "../../etc")
    assert path is None
    assert err is not None and err["status"] == "error"


def test_download_info_to_public_dict_excludes_server_only_fields():
    info = DownloadInfo(
        id="vid1",
        title="Test Video",
        url="https://example.com/watch?v=1",
        quality="best",
        download_type="video",
        codec="auto",
        format="any",
        folder="",
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
