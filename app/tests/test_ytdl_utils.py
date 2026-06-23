"""Tests for pure helpers and migration logic in ``ytdl``."""

from __future__ import annotations

import pickle
import sys
import threading
import types
import unittest

fake_yt_dlp = types.ModuleType("yt_dlp")
fake_networking = types.ModuleType("yt_dlp.networking")
fake_impersonate = types.ModuleType("yt_dlp.networking.impersonate")
fake_utils = types.ModuleType("yt_dlp.utils")


class _ImpersonateTarget:
    @staticmethod
    def from_str(value):
        return value


fake_impersonate.ImpersonateTarget = _ImpersonateTarget
fake_networking.impersonate = fake_impersonate
# The inner ``key`` group mirrors the real ``STR_FORMAT_RE_TMPL`` so that
# ``_OUTTMPL_FIELD_RE`` (compiled at import time) has the named group that
# ``_resolve_outtmpl_fields`` reads via ``match.group('key')``.
fake_utils.STR_FORMAT_RE_TMPL = r"(?P<prefix>)%\((?P<has_key>(?P<key>{}))\)(?P<format>[-0-9.]*{})"
fake_utils.STR_FORMAT_TYPES = "diouxXeEfFgGcrsa"
fake_yt_dlp.networking = fake_networking
fake_yt_dlp.utils = fake_utils
sys.modules.setdefault("yt_dlp", fake_yt_dlp)
sys.modules.setdefault("yt_dlp.networking", fake_networking)
sys.modules.setdefault("yt_dlp.networking.impersonate", fake_impersonate)
sys.modules.setdefault("yt_dlp.utils", fake_utils)

from ytdl import (
    DownloadInfo,
    _compact_persisted_entry,
    _resolve_outtmpl_fields,
    _sanitize_entry_for_pickle,
    _sanitize_path_component,
)

# Detect whether the real yt-dlp is loaded (as opposed to the minimal fake
# shim above).  _resolve_outtmpl_fields needs YoutubeDL at runtime.
_has_real_ytdlp = hasattr(sys.modules.get("yt_dlp"), "YoutubeDL")


class SanitizePathComponentTests(unittest.TestCase):
    def test_replaces_windows_invalid_chars(self):
        self.assertEqual(_sanitize_path_component('a:b*c?d"e<f>g|h'), "a_b_c_d_e_f_g_h")

    def test_non_string_passthrough(self):
        self.assertIs(_sanitize_path_component(None), None)
        self.assertEqual(_sanitize_path_component(42), 42)


@unittest.skipUnless(_has_real_ytdlp, "requires real yt-dlp")
class ResolveOuttmplFieldsTests(unittest.TestCase):
    """Tests for _resolve_outtmpl_fields (delegates to yt-dlp's template engine)."""

    def test_simple_playlist_substitution(self):
        info = {"playlist_title": "My PL", "playlist_index": "03"}
        result = _resolve_outtmpl_fields("%(playlist_title)s/%(title)s.%(ext)s", info, ("playlist",))
        self.assertEqual(result, "My PL/%(title)s.%(ext)s")

    def test_format_spec_int(self):
        info = {"playlist_index": "3"}
        result = _resolve_outtmpl_fields("%(playlist_index)02d-%(title)s", info, ("playlist",))
        self.assertEqual(result, "03-%(title)s")

    def test_non_targeted_fields_unchanged(self):
        info = {"playlist_title": "PL"}
        result = _resolve_outtmpl_fields("%(title)s/%(ext)s", info, ("playlist",))
        self.assertEqual(result, "%(title)s/%(ext)s")

    def test_default_value(self):
        info = {"playlist_index": "1"}
        result = _resolve_outtmpl_fields("%(playlist_title|Unknown)s/%(playlist_index)s", info, ("playlist",))
        self.assertEqual(result, "Unknown/1")

    def test_channel_prefix(self):
        info = {"channel": "MyChan", "channel_index": "05"}
        result = _resolve_outtmpl_fields("%(channel)s/%(channel_index)02d-%(title)s", info, ("channel",))
        self.assertEqual(result, "MyChan/05-%(title)s")

    def test_math_operation(self):
        info = {"playlist_index": "3"}
        result = _resolve_outtmpl_fields("%(playlist_index+100)d", info, ("playlist",))
        self.assertEqual(result, "103")

    def test_playlist_count_and_autonumber(self):
        info = {
            "playlist_title": "My PL",
            "playlist_index": "03",
            "playlist_count": 10,
            "playlist_autonumber": 3,
            "n_entries": 10,
            "__last_playlist_index": 10,
        }
        result = _resolve_outtmpl_fields(
            "%(playlist_title)s/%(playlist_autonumber)s of %(playlist_count)s - %(title)s.%(ext)s",
            info,
            ("playlist",),
        )
        # playlist_autonumber is auto-padded by yt-dlp using __last_playlist_index
        self.assertEqual(result, "My PL/03 of 10 - %(title)s.%(ext)s")

    def test_conditional_playlist_index(self):
        info = {
            "playlist_index": "5",
            "playlist_count": 10,
        }
        result = _resolve_outtmpl_fields(
            "%(playlist_index&{} - |)s%(title)s.%(ext)s",
            info,
            ("playlist",),
        )
        self.assertEqual(result, "5 - %(title)s.%(ext)s")


class SanitizeEntryForPickleTests(unittest.TestCase):
    def test_nested(self):
        def g():
            yield 1

        obj = {"a": g(), "b": [g()]}
        out = _sanitize_entry_for_pickle(obj)
        self.assertEqual(out, {"a": [1], "b": [[1]]})
        pickle.dumps(out)

    def test_plain(self):
        self.assertEqual(_sanitize_entry_for_pickle(5), 5)

    def test_set_converted_to_list(self):
        obj = {"s": {1, 2}}
        out = _sanitize_entry_for_pickle(obj)
        self.assertEqual(sorted(out["s"]), [1, 2])
        pickle.dumps(out)

    def test_map_iterator(self):
        out = _sanitize_entry_for_pickle({"m": map(int, ["1", "2"])})
        self.assertEqual(out, {"m": [1, 2]})

    def test_lock_replaced_with_none(self):
        lock = threading.Lock()
        out = _sanitize_entry_for_pickle({"k": lock})
        self.assertIsNone(out["k"])
        pickle.dumps(out)

    def test_ordered_dict(self):
        from collections import OrderedDict

        od = OrderedDict([("z", 1), ("a", 2)])
        out = _sanitize_entry_for_pickle(od)
        self.assertEqual(out, {"z": 1, "a": 2})


class DownloadInfoSetstateTests(unittest.TestCase):
    def _base_state(self, **kwargs):
        base = {
            "id": "id1",
            "title": "t",
            "url": "http://example.com/v",
            "folder": "",
            "custom_name_prefix": "",
            "error": None,
            "entry": None,
            "playlist_item_limit": 0,
            "msg": None,
            "percent": None,
            "speed": None,
            "eta": None,
            "status": "pending",
            "size": None,
            "timestamp": 0,
        }
        base.update(kwargs)
        return base

    def test_new_state_has_subtitle_files(self):
        state = self._base_state(
            download_type="video",
            codec="auto",
            format="any",
            quality="best",
        )
        di = DownloadInfo.__new__(DownloadInfo)
        di.__setstate__(state)
        self.assertEqual(di.subtitle_files, [])

    def test_missing_optional_fields_are_defaulted(self):
        state = self._base_state(
            download_type="video",
            codec="auto",
            format="any",
            quality="best",
        )
        state.pop("folder")
        state.pop("custom_name_prefix")
        state.pop("playlist_item_limit")
        di = DownloadInfo.__new__(DownloadInfo)
        di.__setstate__(state)
        self.assertEqual(di.folder, "")
        self.assertEqual(di.custom_name_prefix, "")
        self.assertEqual(di.playlist_item_limit, 0)


class CompactPersistedEntryTests(unittest.TestCase):
    def test_keeps_only_playlist_and_channel_keys(self):
        entry = {
            "playlist_index": "01",
            "playlist_title": "Playlist",
            "playlist_count": 10,
            "playlist_autonumber": 1,
            "channel_index": "02",
            "channel_title": "Channel",
            "n_entries": 10,
            "__last_playlist_index": 10,
            "formats": [{"id": "huge"}],
            "description": "big blob",
        }

        compact = _compact_persisted_entry(entry)

        self.assertEqual(
            compact,
            {
                "playlist_index": "01",
                "playlist_title": "Playlist",
                "playlist_count": 10,
                "playlist_autonumber": 1,
                "channel_index": "02",
                "channel_title": "Channel",
                "n_entries": 10,
                "__last_playlist_index": 10,
            },
        )

    def test_returns_none_when_no_restart_relevant_keys_exist(self):
        self.assertIsNone(_compact_persisted_entry({"id": "x", "title": "y"}))


if __name__ == "__main__":
    unittest.main()
