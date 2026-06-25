"""Tests for ``Config`` (env parsing, yt-dlp options)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from main import Config

_BASE_ENV: dict[str, str] = {
    'DOWNLOAD_DIR': '.',
    'TEMP_DIR': '',
    'DELETE_FILE_ON_TRASHCAN': 'false',
    'STATE_DIR': '.',
    'PUBLIC_HOST_URL': 'download/',
    'OUTPUT_TEMPLATE': '%(uploader)s -- @%(extractor)s -- %(title)s -- %(upload_date>%Y-%m-%d)s.%(ext)s',
    'YTDL_OPTIONS': '{}',
    'CORS_ALLOWED_ORIGINS': '',
    'HOST': '0.0.0.0',
    'PORT': '8081',
    'BASE_DIR': '',
    'MAX_CONCURRENT_DOWNLOADS': '3',
    'LOGLEVEL': 'INFO',
    'YTDL_NIGHTLY_UPDATE_TIME': '',
}


def _base_env(**overrides: str) -> dict[str, str]:
    return {**_BASE_ENV, **overrides}


class ConfigTests(unittest.TestCase):
    def test_public_host_url_gets_trailing_slash(self):
        with patch.dict(
            os.environ,
            _base_env(PUBLIC_HOST_URL="https://ytdl.example.com"),
            clear=False,
        ):
            c = Config()
        self.assertEqual(c.PUBLIC_HOST_URL, "https://ytdl.example.com/")

    def test_public_host_url_empty_stays_empty(self):
        with patch.dict(os.environ, _base_env(PUBLIC_HOST_URL=""), clear=False):
            c = Config()
        self.assertEqual(c.PUBLIC_HOST_URL, "")

    def test_public_host_url_already_slashed_unchanged(self):
        with patch.dict(
            os.environ,
            _base_env(PUBLIC_HOST_URL="https://ytdl.example.com/"),
            clear=False,
        ):
            c = Config()
        self.assertEqual(c.PUBLIC_HOST_URL, "https://ytdl.example.com/")

    def test_ytdl_options_json_loaded(self):
        opts = {"quiet": True, "no_warnings": True}
        with patch.dict(
            os.environ,
            _base_env(YTDL_OPTIONS=json.dumps(opts)),
            clear=False,
        ):
            c = Config()
        self.assertEqual(c.YTDL_OPTIONS["quiet"], True)

    def test_ytdl_nightly_update_time_empty_default(self):
        with patch.dict(os.environ, _base_env(YTDL_NIGHTLY_UPDATE_TIME=""), clear=False):
            c = Config()
        self.assertEqual(c.YTDL_NIGHTLY_UPDATE_TIME, "")

    def test_ytdl_nightly_update_time_valid(self):
        with patch.dict(os.environ, _base_env(YTDL_NIGHTLY_UPDATE_TIME="04:00"), clear=False):
            c = Config()
        self.assertEqual(c.YTDL_NIGHTLY_UPDATE_TIME, "04:00")

    def test_ytdl_nightly_update_time_invalid_exits(self):
        for bad in ("25:00", "4am", "12:60"):
            with patch.dict(os.environ, _base_env(YTDL_NIGHTLY_UPDATE_TIME=bad), clear=False):
                with self.assertRaises(ValidationError):
                    Config()




if __name__ == "__main__":
    unittest.main()
