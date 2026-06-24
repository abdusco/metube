"""Tests for pure helpers in ``main`` (logging, config, request models)."""

from __future__ import annotations

import logging
import unittest

from pydantic import ValidationError

import main


class ParseLogLevelTests(unittest.TestCase):
    def test_valid_levels(self):
        self.assertEqual(main.parse_log_level("INFO"), logging.INFO)
        self.assertEqual(main.parse_log_level("debug"), logging.DEBUG)

    def test_invalid_returns_none(self):
        self.assertIsNone(main.parse_log_level("not_a_level"))
        self.assertIsNone(main.parse_log_level(123))


class AddRequestTests(unittest.TestCase):
    _base: dict = {
        "url": "https://example.com/v",
        "download_type": "video",
        "codec": "auto",
        "format": "any",
        "quality": "best",
    }

    def test_valid_video(self):
        req = main.AddJobRequest.model_validate(self._base)
        self.assertEqual(req.download_type, "video")
        self.assertEqual(req.quality, "best")

    def test_rejects_empty_url(self):
        with self.assertRaises(ValidationError):
            main.AddJobRequest.model_validate({**self._base, "url": ""})

    def test_rejects_invalid_download_type(self):
        with self.assertRaises(ValidationError):
            main.AddJobRequest.model_validate({**self._base, "download_type": "zip"})

    def test_rejects_invalid_subtitle_lang(self):
        with self.assertRaises(ValidationError):
            main.AddJobRequest.model_validate({**self._base, "subtitle_langs": [""]})

    def test_audio_forces_auto_codec(self):
        req = main.AddJobRequest.model_validate({
            "url": "https://example.com/v",
            "download_type": "audio",
            "codec": "h264",
            "format": "mp3",
            "quality": "best",
        })
        self.assertEqual(req.codec, "auto")

if __name__ == "__main__":
    unittest.main()
