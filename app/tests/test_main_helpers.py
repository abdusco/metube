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


class FrontendSafeTests(unittest.TestCase):
    def test_only_expected_keys(self):
        safe = main.config.frontend_safe()
        for key in main.Config._FRONTEND_KEYS:
            self.assertIn(key, safe)
        self.assertNotIn("YTDL_OPTIONS", safe)
        self.assertNotIn("DOWNLOAD_DIR", safe)
        self.assertIn("ALLOW_YTDL_OPTIONS_OVERRIDES", safe)


class AddRequestTests(unittest.TestCase):
    _base: dict = {
        "url": "https://example.com/v",
        "download_type": "video",
        "codec": "auto",
        "format": "any",
        "quality": "best",
    }

    def test_valid_video(self):
        req = main.AddRequest.model_validate(self._base)
        self.assertEqual(req.download_type, "video")
        self.assertEqual(req.quality, "best")

    def test_rejects_empty_url(self):
        with self.assertRaises(ValidationError):
            main.AddRequest.model_validate({**self._base, "url": ""})

    def test_rejects_invalid_download_type(self):
        with self.assertRaises(ValidationError):
            main.AddRequest.model_validate({**self._base, "download_type": "zip"})

    def test_rejects_bad_custom_name_prefix(self):
        with self.assertRaises(ValidationError):
            main.AddRequest.model_validate({**self._base, "custom_name_prefix": "../secret"})

    def test_rejects_invalid_subtitle_lang(self):
        with self.assertRaises(ValidationError):
            main.AddRequest.model_validate({**self._base, "subtitle_langs": ["!bad"]})

    def test_rejects_non_object_overrides_json(self):
        with self.assertRaises(ValidationError):
            main.AddRequest.model_validate({**self._base, "ytdl_options_overrides": '["bad"]'})

    def test_rejects_invalid_overrides_json(self):
        with self.assertRaises(ValidationError):
            main.AddRequest.model_validate({**self._base, "ytdl_options_overrides": "not-json"})

    def test_parses_string_overrides(self):
        req = main.AddRequest.model_validate({**self._base, "ytdl_options_overrides": '{"quiet": true}'})
        self.assertEqual(req.ytdl_options_overrides, {"quiet": True})

    def test_empty_overrides_string_becomes_empty_dict(self):
        req = main.AddRequest.model_validate({**self._base, "ytdl_options_overrides": ""})
        self.assertEqual(req.ytdl_options_overrides, {})

    def test_legacy_singular_preset_normalized(self):
        req = main.AddRequest.model_validate({**self._base, "ytdl_options_preset": "Solo"})
        self.assertEqual(req.ytdl_options_presets, ["Solo"])

    def test_multiple_presets_list(self):
        req = main.AddRequest.model_validate({**self._base, "ytdl_options_presets": ["A", "B"]})
        self.assertEqual(req.ytdl_options_presets, ["A", "B"])

    def test_captions_forces_best_quality(self):
        req = main.AddRequest.model_validate({**self._base, "download_type": "captions", "quality": "720"})
        self.assertEqual(req.quality, "best")

    def test_thumbnail_forces_best_quality(self):
        req = main.AddRequest.model_validate({**self._base, "download_type": "thumbnail", "quality": "720"})
        self.assertEqual(req.quality, "best")

    def test_audio_forces_auto_codec(self):
        req = main.AddRequest.model_validate({
            "url": "https://example.com/v",
            "download_type": "audio",
            "codec": "h264",
            "format": "mp3",
            "quality": "best",
        })
        self.assertEqual(req.codec, "auto")

    def test_audio_rejects_invalid_format(self):
        with self.assertRaises(ValidationError):
            main.AddRequest.model_validate({
                "url": "https://example.com/v",
                "download_type": "audio",
                "codec": "auto",
                "format": "mp4",
                "quality": "best",
            })

    def test_video_rejects_invalid_quality(self):
        with self.assertRaises(ValidationError):
            main.AddRequest.model_validate({**self._base, "quality": "999p"})

    def test_playlist_item_limit_defaults_to_none(self):
        req = main.AddRequest.model_validate(self._base)
        self.assertIsNone(req.playlist_item_limit)


if __name__ == "__main__":
    unittest.main()
