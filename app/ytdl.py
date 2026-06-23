import os
import yt_dlp
import collections
import collections.abc
import copy
import pickle
from collections import OrderedDict
from pathlib import Path
import time
import asyncio
import multiprocessing
from functools import partial
import logging
import re
import types
from typing import Any, Optional

import yt_dlp.networking.impersonate
from yt_dlp.utils import STR_FORMAT_RE_TMPL, STR_FORMAT_TYPES
from dl_formats import get_format, get_opts, AUDIO_FORMATS
from state_store import AtomicJsonStore, from_json_compatible, to_json_compatible


def _entry_id(entry: dict) -> Optional[str]:
    eid = entry.get("id")
    if eid is not None:
        return str(eid)
    return entry.get("webpage_url") or entry.get("url")

log = logging.getLogger('ytdl')

# Characters that are invalid in Windows/NTFS path components. These are pre-
# sanitised when substituting playlist/channel titles into output templates so
# that downloads do not fail on NTFS-mounted volumes or Windows Docker hosts.
_WINDOWS_INVALID_PATH_CHARS = re.compile(r'[\\:*?"<>|]')


def _sanitize_path_component(value: Any) -> Any:
    """Replace characters that are invalid in Windows path components with '_'.

    Non-string values (int, float, None, …) are passed through unchanged so
    that numeric format specs (e.g. ``%(playlist_index)02d``) still work.
    Only string values are sanitised because Windows-invalid characters are
    only a concern for human-readable strings (titles, channel names, etc.)
    that may end up as directory names.
    """
    if not isinstance(value, str):
        return value
    return _WINDOWS_INVALID_PATH_CHARS.sub('_', value)


# Regex matching yt-dlp output-template field references, e.g. ``%(title)s``
# or ``%(playlist_index)03d``.  Built from yt-dlp's own ``STR_FORMAT_RE_TMPL``
# so that it stays in sync with upstream changes to the template syntax.
_OUTTMPL_FIELD_RE = re.compile(
    STR_FORMAT_RE_TMPL.format('[^)]+', f'[{STR_FORMAT_TYPES}ljhqBUDS]')
)


def _resolve_outtmpl_fields(template: str, info_dict: dict, prefixes: tuple[str, ...]) -> str:
    """Resolve specific fields in an output template using yt-dlp's template engine.

    Only field references whose root name starts with one of *prefixes* are
    evaluated.  All other references are left untouched so that yt-dlp can
    resolve them later during the actual download.

    This delegates to ``YoutubeDL.evaluate_outtmpl`` for each targeted field
    reference, giving access to the full yt-dlp template syntax (defaults,
    conditional formatting, math operations, datetime formatting, etc.).
    """
    matches = list(_OUTTMPL_FIELD_RE.finditer(template))
    if not matches:
        return template

    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        for match in reversed(matches):
            key = match.group('key')
            if key is None:
                continue
            root = re.match(r'\w+', key)
            if root is None or not root.group(0).startswith(prefixes):
                continue
            resolved = ydl.evaluate_outtmpl(match.group(0), info_dict)
            template = template[:match.start()] + resolved + template[match.end():]

    return template

_MAX_ENTRY_SANITIZE_DEPTH = 64


def _sanitize_entry_for_pickle(obj, _depth=0):
    """Recursively normalize yt-dlp ``info_dict`` data so it can be stored in shelve/pickle.

    Live streams and newer yt-dlp versions may nest generators, iterators, sets, or
    non-serializable objects (e.g. locks) inside the extracted metadata. The previous
    helper only walked plain dict/list/tuple and only expanded ``types.GeneratorType``.
    """
    if _depth > _MAX_ENTRY_SANITIZE_DEPTH:
        return None
    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return obj
    if isinstance(obj, types.GeneratorType):
        return _sanitize_entry_for_pickle(list(obj), _depth + 1)
    if isinstance(obj, collections.abc.Mapping):
        return {k: _sanitize_entry_for_pickle(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_sanitize_entry_for_pickle(x, _depth + 1) for x in obj)
    if isinstance(obj, (set, frozenset)):
        return [_sanitize_entry_for_pickle(x, _depth + 1) for x in obj]
    if isinstance(obj, collections.deque):
        return [_sanitize_entry_for_pickle(x, _depth + 1) for x in obj]
    if isinstance(obj, collections.abc.Iterator):
        try:
            return _sanitize_entry_for_pickle(list(obj), _depth + 1)
        except Exception:
            return None
    try:
        pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        return obj
    except Exception:
        return None


class DownloadQueueNotifier:
    async def added(self, dl):
        raise NotImplementedError

    async def updated(self, dl):
        raise NotImplementedError

    async def completed(self, dl):
        raise NotImplementedError

    async def canceled(self, id):
        raise NotImplementedError

    async def cleared(self, id):
        raise NotImplementedError

class DownloadInfo:
    def __init__(
        self,
        id,
        title,
        url,
        quality,
        download_type,
        codec,
        format,
        folder,
        custom_name_prefix,
        error,
        entry,
        playlist_item_limit,
        subtitle_langs=None,
        ytdl_options_presets=None,
        ytdl_options_overrides=None,
    ):
        self.id = id if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{id}'
        self.title = title if len(custom_name_prefix) == 0 else f'{custom_name_prefix}.{title}'
        self.url = url
        self.quality = quality
        self.download_type = download_type
        self.codec = codec
        self.format = format
        self.folder = folder
        self.custom_name_prefix = custom_name_prefix
        self.msg = self.percent = self.speed = self.eta = None
        self.status = "pending"
        self.size = None
        self.timestamp = time.time_ns()
        self.error = error
        # Strip non-pickleable values (generators, iterators, locks, etc.) for shelve
        self.entry = _sanitize_entry_for_pickle(entry) if entry is not None else None
        self.playlist_item_limit = playlist_item_limit
        self.subtitle_langs = list(subtitle_langs) if subtitle_langs else []
        self.ytdl_options_presets = list(ytdl_options_presets or [])
        self.ytdl_options_overrides = dict(ytdl_options_overrides or {})
        self.subtitle_files = []
        self.logs: list = []

    # Fields excluded from the client-facing /queue response.
    # ``entry`` is the full yt-dlp info-dict (potentially large, re-sent on every
    # progress tick); ``logs`` is fetched separately via GET /logs.
    _PUBLIC_EXCLUDED_FIELDS = ("entry", "logs")

    def to_public_dict(self) -> dict:
        """Return the client-facing view, omitting server-only/bulky fields."""
        return {
            k: v
            for k, v in self.__dict__.items()
            if k not in self._PUBLIC_EXCLUDED_FIELDS
        }

    def __setstate__(self, state):
        self.__dict__.update(state)
        if not getattr(self, "codec", None):
            self.codec = "auto"
        if not hasattr(self, "folder"):
            self.folder = ""
        if not hasattr(self, "custom_name_prefix"):
            self.custom_name_prefix = ""
        if not hasattr(self, "playlist_item_limit"):
            self.playlist_item_limit = 0
        if not hasattr(self, "subtitle_langs"):
            self.subtitle_langs = []
        legacy_preset = self.__dict__.pop("ytdl_options_preset", None)
        if "ytdl_options_presets" not in self.__dict__:
            if isinstance(legacy_preset, str) and legacy_preset.strip():
                self.ytdl_options_presets = [legacy_preset.strip()]
            elif isinstance(legacy_preset, list):
                self.ytdl_options_presets = [str(x).strip() for x in legacy_preset if str(x).strip()]
            else:
                self.ytdl_options_presets = []
        if not hasattr(self, "ytdl_options_overrides"):
            self.ytdl_options_overrides = {}
        if not hasattr(self, "entry"):
            self.entry = None
        if not hasattr(self, "subtitle_files"):
            self.subtitle_files = []
        if not hasattr(self, "logs"):
            self.logs = []


_PERSISTED_DOWNLOAD_FIELDS = (
    "id",
    "title",
    "url",
    "quality",
    "download_type",
    "codec",
    "format",
    "folder",
    "custom_name_prefix",
    "playlist_item_limit",
    "subtitle_langs",
    "ytdl_options_presets",
    "ytdl_options_overrides",
    "status",
    "timestamp",
    "error",
    "msg",
    "filename",
    "size",
    "subtitle_files",
)


_COMPACT_ENTRY_EXTRA_KEYS = frozenset(("n_entries", "__last_playlist_index"))


def _compact_persisted_entry(entry: Any) -> Optional[dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    compact = {
        key: value
        for key, value in entry.items()
        if key.startswith("playlist") or key.startswith("channel") or key in _COMPACT_ENTRY_EXTRA_KEYS
    }
    return compact or None


def _download_info_to_record(
    info: DownloadInfo,
    *,
    include_entry: bool,
) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key in _PERSISTED_DOWNLOAD_FIELDS:
        if hasattr(info, key):
            value = getattr(info, key)
            if value is not None:
                record[key] = to_json_compatible(value)
    if include_entry:
        compact_entry = _compact_persisted_entry(getattr(info, "entry", None))
        if compact_entry is not None:
            record["entry"] = to_json_compatible(compact_entry)
    return record


def _download_info_from_record(record: dict[str, Any]) -> DownloadInfo:
    info = DownloadInfo.__new__(DownloadInfo)
    info.__setstate__({key: from_json_compatible(value) for key, value in record.items()})
    if not hasattr(info, "msg"):
        info.msg = None
    if not hasattr(info, "percent"):
        info.percent = None
    if not hasattr(info, "speed"):
        info.speed = None
    if not hasattr(info, "eta"):
        info.eta = None
    if not hasattr(info, "status"):
        info.status = "pending"
    if not hasattr(info, "size"):
        info.size = None
    if not hasattr(info, "error"):
        info.error = None
    return info

class Download:
    manager = None

    @classmethod
    def shutdown_manager(cls):
        if cls.manager is not None:
            cls.manager.shutdown()
            cls.manager = None

    def __init__(self, download_dir, temp_dir, output_template, quality, format, ytdl_opts, info):
        self.download_dir = download_dir
        self.temp_dir = temp_dir
        self.output_template = output_template
        self.info = info
        self.format = get_format(
            getattr(info, 'download_type', 'video'),
            getattr(info, 'codec', 'auto'),
            format,
            quality,
        )
        self.ytdl_opts = get_opts(
            getattr(info, 'download_type', 'video'),
            getattr(info, 'codec', 'auto'),
            format,
            quality,
            ytdl_opts,
            subtitle_langs=getattr(info, 'subtitle_langs', []),
        )
        if "impersonate" in self.ytdl_opts:
            self.ytdl_opts["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(self.ytdl_opts["impersonate"])
        self.canceled = False
        self.tmpfilename = None
        self.status_queue = None
        self.proc = None
        self.loop = None
        self.notifier = None

    def _download(self):
        log.info(f"Starting download for: {self.info.title} ({self.info.url})")
        try:
            debug_logging = logging.getLogger().isEnabledFor(logging.DEBUG)
            def put_status(st):
                self.status_queue.put({k: v for k, v in st.items() if k in (
                    'tmpfilename',
                    'filename',
                    'status',
                    'msg',
                    'total_bytes',
                    'total_bytes_estimate',
                    'downloaded_bytes',
                    'speed',
                    'eta',
                )})

            def put_status_postprocessor(d):
                if d['postprocessor'] == 'MoveFiles' and d['status'] == 'finished':
                    filepath = d['info_dict']['filepath']
                    if '__finaldir' in d['info_dict']:
                        finaldir = d['info_dict']['__finaldir']
                        filename = str(Path(finaldir) / Path(filepath).name)
                    else:
                        filename = filepath
                    self.status_queue.put({'status': 'finished', 'filename': filename})
                    # Capture subtitle files from any download type that requested them.
                    requested_subtitles = d.get('info_dict', {}).get('requested_subtitles', {}) or {}
                    for subtitle in requested_subtitles.values():
                        if isinstance(subtitle, dict) and subtitle.get('filepath'):
                            self.status_queue.put({'subtitle_file': subtitle['filepath']})

            _sq = self.status_queue
            class _YtdlLogger:
                def debug(self, msg):
                    # yt-dlp prefixes verbose-only lines with '[debug] '; skip unless verbose mode
                    if msg.startswith('[debug] ') and not debug_logging:
                        return
                    _sq.put({'log': msg})
                def info(self, msg):  # yt-dlp never calls this, kept for completeness
                    _sq.put({'log': msg})
                def warning(self, msg):
                    _sq.put({'log': f'[WARNING] {msg}'})
                def error(self, msg):
                    _sq.put({'log': f'[ERROR] {msg}'})

            ytdl_params = {
                **self.ytdl_opts,
                'quiet': not debug_logging,
                'verbose': debug_logging,
                'no_color': True,
                'paths': {"home": self.download_dir, "temp": self.temp_dir},
                'outtmpl': { "default": self.output_template },
                'format': self.format,
                'socket_timeout': 30,
                'ignore_no_formats_error': True,
                'progress_hooks': [put_status],
                'postprocessor_hooks': [put_status_postprocessor],
                'logger': _YtdlLogger(),
            }

            ret = yt_dlp.YoutubeDL(params=ytdl_params).download([self.info.url])
            self.status_queue.put({'status': 'finished' if ret == 0 else 'error'})
            log.info(f"Finished download for: {self.info.title}")
        except yt_dlp.utils.YoutubeDLError as exc:
            log.error(f"Download error for {self.info.title}: {str(exc)}")
            self.status_queue.put({'status': 'error', 'msg': str(exc)})

    async def start(self, notifier):
        log.info(f"Preparing download for: {self.info.title}")
        if Download.manager is None:
            Download.manager = multiprocessing.Manager()
        self.status_queue = Download.manager.Queue()
        self.proc = multiprocessing.Process(target=self._download)
        self.proc.start()
        self.loop = asyncio.get_running_loop()
        self.notifier = notifier
        self.info.status = 'preparing'
        await self.notifier.updated(self.info)
        self.status_task = asyncio.create_task(self.update_status())
        await self.loop.run_in_executor(None, self.proc.join)
        # Signal update_status to stop and wait for it to finish
        # so that all status updates (including MoveFiles with correct
        # file size) are processed before _post_download_cleanup runs.
        if self.status_queue is not None:
            self.status_queue.put(None)
        await self.status_task

    def cancel(self):
        log.info(f"Cancelling download: {self.info.title}")
        if self.running():
            try:
                self.proc.kill()
            except Exception as e:
                log.error(f"Error killing process for {self.info.title}: {e}")
        self.canceled = True
        if self.status_queue is not None:
            self.status_queue.put(None)

    def close(self):
        log.info(f"Closing download process for: {self.info.title}")
        if self.started():
            self.proc.close()

    def running(self):
        try:
            return self.proc is not None and self.proc.is_alive()
        except ValueError:
            return False

    def started(self):
        return self.proc is not None

    async def update_status(self):
        while True:
            status = await self.loop.run_in_executor(None, self.status_queue.get)
            if status is None:
                log.info(f"Status update finished for: {self.info.title}")
                return
            if self.canceled:
                log.info(f"Download {self.info.title} is canceled; stopping status updates.")
                return
            if 'log' in status:
                self.info.logs.append(status['log'])
                continue
            self.tmpfilename = status.get('tmpfilename')
            if 'filename' in status:
                fileName = status.get('filename')
                rel_name = os.path.relpath(fileName, self.download_dir)
                # For captions mode, ignore media-like placeholders and let subtitle_file
                # statuses define the final file shown in the UI.
                if getattr(self.info, 'download_type', '') == 'captions':
                    if not rel_name.lower().endswith(('.vtt', '.srt', '.sbv', '.scc', '.ttml', '.dfxp')):
                        continue
                self.info.filename = rel_name
                _fn = Path(fileName)
                self.info.size = _fn.stat().st_size if _fn.exists() else None
                if getattr(self.info, 'download_type', '') == 'thumbnail':
                    # The thumbnail convertor always emits a .jpg, but yt-dlp may
                    # report the pre-conversion media/thumbnail extension
                    # (.webm/.mp4/.png/.webp/...). Normalise to .jpg regardless.
                    self.info.filename = str(Path(self.info.filename).with_suffix('.jpg'))

            log.debug(f"Update status for {self.info.title}: {status}")
            if 'subtitle_file' in status:
                subtitle_file = status.get('subtitle_file')
                if not subtitle_file:
                    continue
                rel_path = os.path.relpath(subtitle_file, self.download_dir)
                _sf = Path(subtitle_file)
                file_size = _sf.stat().st_size if _sf.exists() else None
                existing = next((sf for sf in self.info.subtitle_files if sf['filename'] == rel_path), None)
                if not existing:
                    self.info.subtitle_files.append({'filename': rel_path, 'size': file_size})
                # Prefer first subtitle file as the primary result link in captions mode.
                if getattr(self.info, 'download_type', '') == 'captions' and not getattr(self.info, 'filename', None):
                    self.info.filename = rel_path
                    self.info.size = file_size
                continue

            self.info.status = status['status']
            self.info.msg = status.get('msg')
            if 'downloaded_bytes' in status:
                total = status.get('total_bytes') or status.get('total_bytes_estimate')
                if total:
                    self.info.percent = status['downloaded_bytes'] / total * 100
            self.info.speed = status.get('speed')
            self.info.eta = status.get('eta')
            log.debug(f"Updating status for {self.info.title}: {status}")
            await self.notifier.updated(self.info)

class PersistentQueue:
    def __init__(self, name, path):
        self.identifier = name
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path.with_suffix('.json'))
        self.store = AtomicJsonStore(self.path, kind=f"persistent_queue:{name}")
        self.dict = OrderedDict()

    def load(self):
        for k, v in self.saved_items():
            self.dict[k] = Download(None, None, None, getattr(v, 'quality', 'best'), getattr(v, 'format', 'any'), {}, v)

    def exists(self, key):
        return key in self.dict

    def get(self, key):
        return self.dict[key]

    def items(self):
        return self.dict.items()

    def saved_items(self):
        items = [
            (item["key"], _download_info_from_record(item["info"]))
            for item in self._load_state_items()
        ]
        return sorted(items, key=lambda item: item[1].timestamp)

    def _should_persist_entry(self) -> bool:
        return self.identifier != "completed"

    def _serialize_items(self):
        return [
            {
                "key": key,
                "info": _download_info_to_record(
                    download.info,
                    include_entry=self._should_persist_entry(),
                ),
            }
            for key, download in self.dict.items()
        ]

    def _save_dict(self):
        self.store.save({"items": self._serialize_items()})

    def _load_state_items(self):
        payload = self.store.load()
        if payload is not None:
            items = payload.get("items")
            if isinstance(items, list):
                compact_items = [
                    {
                        "key": item["key"],
                        "info": _download_info_to_record(
                            _download_info_from_record(item["info"]),
                            include_entry=self._should_persist_entry(),
                        ),
                    }
                    for item in items
                    if isinstance(item, dict) and "key" in item and "info" in item
                ]
                if payload.get("schema_version") != self.store.schema_version or compact_items != items:
                    self.store.save({"items": compact_items})
                return compact_items
            log.warning("PersistentQueue:%s state file did not contain an items list", self.identifier)
        return []

    def put(self, value):
        key = value.info.url
        old = self.dict.get(key)
        self.dict[key] = value
        try:
            self._save_dict()
        except Exception:
            if old is None:
                del self.dict[key]
            else:
                self.dict[key] = old
            raise

    def delete(self, key):
        if key in self.dict:
            old = self.dict[key]
            del self.dict[key]
            try:
                self._save_dict()
            except Exception:
                self.dict[key] = old
                raise

    def next(self):
        k, v = next(iter(self.dict.items()))
        return k, v

    def empty(self):
        return not bool(self.dict)

class DownloadQueue:
    def __init__(self, config, notifier):
        self.config = config
        self.notifier = notifier
        _state = Path(self.config.STATE_DIR)
        self.queue = PersistentQueue("queue", _state / 'queue')
        self.done = PersistentQueue("completed", _state / 'completed')
        self.pending = PersistentQueue("pending", _state / 'pending')
        self.active_downloads = set()
        self.semaphore = asyncio.Semaphore(int(self.config.MAX_CONCURRENT_DOWNLOADS))
        self.done.load()
        self._add_generation = 0
        self._canceled_urls = set()  # URLs canceled during current playlist add

    def cancel_add(self):
        self._add_generation += 1
        log.info('Playlist add operation canceled by user')

    async def __import_queue(self):
        for k, v in self.queue.saved_items():
            await self.__add_download(v, True)

    async def __import_pending(self):
        for k, v in self.pending.saved_items():
            await self.__add_download(v, False)

    async def initialize(self):
        log.info("Initializing DownloadQueue")
        asyncio.create_task(self.__import_queue())
        asyncio.create_task(self.__import_pending())

    async def __start_download(self, download):
        if download.canceled:
            log.info(f"Download {download.info.title} was canceled, skipping start.")
            return
        async with self.semaphore:
            if download.canceled:
                log.info(f"Download {download.info.title} was canceled, skipping start.")
                return
            await download.start(self.notifier)
            self._post_download_cleanup(download)

    def _post_download_cleanup(self, download):
        if download.info.status != 'finished':
            if download.tmpfilename and Path(download.tmpfilename).is_file():
                try:
                    Path(download.tmpfilename).unlink()
                except OSError:
                    pass
            download.info.status = 'error'
        download.close()
        if self.queue.exists(download.info.url):
            self.queue.delete(download.info.url)
            if download.canceled:
                asyncio.create_task(self.notifier.canceled(download.info.url))
            else:
                self.done.put(download)
                asyncio.create_task(self.notifier.completed(download.info))
                try:
                    clear_after = int(self.config.CLEAR_COMPLETED_AFTER)
                except ValueError:
                    log.error(f'CLEAR_COMPLETED_AFTER is set to an invalid value "{self.config.CLEAR_COMPLETED_AFTER}", expected an integer number of seconds')
                    clear_after = 0
                if clear_after > 0:
                    task = asyncio.create_task(self.__auto_clear_after_delay(download.info.url, clear_after))
                    task.add_done_callback(lambda t: log.error(f'Auto-clear task failed: {t.exception()}') if not t.cancelled() and t.exception() else None)

    async def __auto_clear_after_delay(self, url, delay_seconds):
        await asyncio.sleep(delay_seconds)
        if self.done.exists(url):
            log.debug(f'Auto-clearing completed download: {url}')
            await self.clear([url])

    def _build_ytdl_options(self, ytdl_options_presets=None, ytdl_options_overrides=None):
        """Merge global options, presets (in order), and per-download overrides."""
        opts = dict(self.config.YTDL_OPTIONS)
        for preset_name in ytdl_options_presets or []:
            opts.update(self.config.YTDL_OPTIONS_PRESETS.get(preset_name, {}))
        opts.update(ytdl_options_overrides or {})
        return opts

    def __extract_info(self, url, ytdl_options_presets=None, ytdl_options_overrides=None):
        debug_logging = logging.getLogger().isEnabledFor(logging.DEBUG)
        user_opts = self._build_ytdl_options(ytdl_options_presets, ytdl_options_overrides)
        params = {
            **user_opts,
            'quiet': not debug_logging,
            'verbose': debug_logging,
            'no_color': True,
            'extract_flat': True,
            'ignore_no_formats_error': True,
            'noplaylist': True,
            'paths': {"home": self.config.DOWNLOAD_DIR, "temp": self.config.TEMP_DIR},
        }
        imp = user_opts.get('impersonate')
        if imp is not None:
            params['impersonate'] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(imp)
        return yt_dlp.YoutubeDL(params=params).extract_info(url, download=False)

    def __calc_download_path(self, download_type, folder):
        base_directory = self.config.AUDIO_DOWNLOAD_DIR if download_type == 'audio' else self.config.DOWNLOAD_DIR
        if folder:
            real_base = Path(base_directory).resolve()
            dldirectory = (Path(base_directory) / folder).resolve()
            if not dldirectory.is_relative_to(real_base):
                return None, {'status': 'error', 'msg': f'Folder "{folder}" must resolve inside the base download directory "{real_base}"'}
            dldirectory.mkdir(parents=True, exist_ok=True)
            return str(dldirectory), None
        else:
            dldirectory = base_directory
        return dldirectory, None

    async def __add_download(self, dl, auto_start):
        dldirectory, error_message = self.__calc_download_path(dl.download_type, dl.folder)
        if error_message is not None:
            return error_message
        output = self.config.OUTPUT_TEMPLATE if len(dl.custom_name_prefix) == 0 else f'{dl.custom_name_prefix}.{self.config.OUTPUT_TEMPLATE}'
        entry = getattr(dl, 'entry', None)
        if entry is not None and entry.get('playlist_index') is not None:
            if len(self.config.OUTPUT_TEMPLATE_PLAYLIST):
                output = self.config.OUTPUT_TEMPLATE_PLAYLIST
            sanitized = {k: _sanitize_path_component(v) for k, v in entry.items()}
            output = _resolve_outtmpl_fields(output, sanitized, ('playlist',))
        if entry is not None and entry.get('channel_index') is not None:
            if len(self.config.OUTPUT_TEMPLATE_CHANNEL):
                output = self.config.OUTPUT_TEMPLATE_CHANNEL
            sanitized = {k: _sanitize_path_component(v) for k, v in entry.items()}
            output = _resolve_outtmpl_fields(output, sanitized, ('channel',))
        ytdl_options = self._build_ytdl_options(
            getattr(dl, 'ytdl_options_presets', None),
            getattr(dl, 'ytdl_options_overrides', {}) or {},
        )
        playlist_item_limit = getattr(dl, 'playlist_item_limit', 0)
        if playlist_item_limit > 0:
            log.info(f'playlist limit is set. Processing only first {playlist_item_limit} entries')
            ytdl_options['playlistend'] = playlist_item_limit
        download = Download(dldirectory, self.config.TEMP_DIR, output, dl.quality, dl.format, ytdl_options, dl)
        if auto_start is True:
            self.queue.put(download)
            asyncio.create_task(self.__start_download(download))
        else:
            self.pending.put(download)
        await self.notifier.added(dl)

    async def __add_entry(
        self,
        entry,
        download_type,
        codec,
        format,
        quality,
        folder,
        custom_name_prefix,
        playlist_item_limit,
        auto_start,
        subtitle_langs,
        ytdl_options_presets,
        ytdl_options_overrides,
        already,
        _add_gen=None,
    ):
        if not entry:
            return {'status': 'error', 'msg': "Invalid/empty data was given."}

        error = entry.get("msg") if entry else None

        etype = entry.get('_type') or 'video'

        if etype.startswith('url'):
            log.debug('Processing as a url')
            return await self.add(
                entry['url'],
                download_type,
                codec,
                format,
                quality,
                folder,
                custom_name_prefix,
                playlist_item_limit,
                auto_start,
                subtitle_langs,
                ytdl_options_presets,
                ytdl_options_overrides,
                already,
                _add_gen,
            )
        elif etype == 'playlist' or etype == 'channel':
            log.debug(f'Processing as a {etype}')
            entries = entry['entries']
            # Convert generator to list if needed (for len() and slicing operations)
            if isinstance(entries, types.GeneratorType):
                entries = list(entries)
            total_entries = len(entries)
            log.info(f'{etype} detected with {total_entries} entries')
            index_digits = len(str(total_entries))
            results = []
            if playlist_item_limit > 0:
                log.info(f'Item limit is set. Processing only first {playlist_item_limit} entries')
                entries = entries[:playlist_item_limit]
            for index, etr in enumerate(entries, start=1):
                if _add_gen is not None and self._add_generation != _add_gen:
                    log.info(f'Playlist add canceled after processing {len(already)} entries')
                    return {'status': 'ok', 'msg': f'Canceled - added {len(already)} items before cancel'}
                if "id" not in etr:
                    etr["id"] = _entry_id(etr)
                etr["_type"] = "video"
                etr[etype] = entry.get("id") or entry.get("channel_id") or entry.get("channel")
                etr[f"{etype}_index"] = '{{0:0{0:d}d}}'.format(index_digits).format(index)
                etr[f"{etype}_count"] = total_entries
                etr[f"{etype}_autonumber"] = index
                # n_entries: standard yt-dlp field for total count (used by template engine)
                # __last_playlist_index: yt-dlp internal field for auto-padding autonumber
                etr["n_entries"] = total_entries
                etr["__last_playlist_index"] = total_entries
                for property in ("id", "title", "uploader", "uploader_id"):
                    if property in entry:
                        etr[f"{etype}_{property}"] = entry[property]
                results.append(
                    await self.__add_entry(
                        etr,
                        download_type,
                        codec,
                        format,
                        quality,
                        folder,
                        custom_name_prefix,
                        playlist_item_limit,
                        auto_start,
                        subtitle_langs,
                        ytdl_options_presets,
                        ytdl_options_overrides,
                        already,
                        _add_gen,
                    )
                )
            if any(res['status'] == 'error' for res in results):
                return {'status': 'error', 'msg': ', '.join(res['msg'] for res in results if res['status'] == 'error' and 'msg' in res)}
            return {'status': 'ok'}
        elif etype == 'video' or (etype.startswith('url') and 'id' in entry and 'title' in entry):
            log.debug('Processing as a video')
            key = entry.get('webpage_url') or entry['url']
            if key in self._canceled_urls:
                log.info(f'Skipping canceled URL: {entry.get("title") or key}')
                return {'status': 'ok'}
            if not self.queue.exists(key):
                dl = DownloadInfo(
                    id=entry['id'],
                    title=entry.get('title') or entry['id'],
                    url=key,
                    quality=quality,
                    download_type=download_type,
                    codec=codec,
                    format=format,
                    folder=folder,
                    custom_name_prefix=custom_name_prefix,
                    error=error,
                    entry=entry,
                    playlist_item_limit=playlist_item_limit,
                    subtitle_langs=subtitle_langs,
                    ytdl_options_presets=ytdl_options_presets,
                    ytdl_options_overrides=ytdl_options_overrides,
                )
                await self.__add_download(dl, auto_start)
            return {'status': 'ok'}
        return {'status': 'error', 'msg': f'Unsupported resource "{etype}"'}

    async def add(
        self,
        url,
        download_type,
        codec,
        format,
        quality,
        folder,
        custom_name_prefix,
        playlist_item_limit,
        auto_start=True,
        subtitle_langs=None,
        ytdl_options_presets=None,
        ytdl_options_overrides=None,
        already=None,
        _add_gen=None,
    ):
        if ytdl_options_presets is None:
            ytdl_options_presets = []
        log.info(
            f'adding {url}: {download_type=} {codec=} {format=} {quality=} {already=} {folder=} {custom_name_prefix=} '
            f'{playlist_item_limit=} {auto_start=} {subtitle_langs=} {ytdl_options_presets=}'
        )
        if already is None:
            _add_gen = self._add_generation
            self._canceled_urls.clear()
        already = set() if already is None else already
        if url in already:
            log.info('recursion detected, skipping')
            return {'status': 'ok'}
        else:
            already.add(url)
        try:
            entry = await asyncio.get_running_loop().run_in_executor(
                None,
                partial(self.__extract_info, url, ytdl_options_presets, ytdl_options_overrides),
            )
        except yt_dlp.utils.YoutubeDLError as exc:
            return {'status': 'error', 'msg': str(exc)}
        return await self.__add_entry(
            entry,
            download_type,
            codec,
            format,
            quality,
            folder,
            custom_name_prefix,
            playlist_item_limit,
            auto_start,
            subtitle_langs or [],
            ytdl_options_presets,
            ytdl_options_overrides,
            already,
            _add_gen,
        )

    async def add_entry(
        self,
        entry,
        download_type,
        codec,
        format,
        quality,
        folder,
        custom_name_prefix,
        playlist_item_limit,
        auto_start=True,
        subtitle_langs=None,
        ytdl_options_presets=None,
        ytdl_options_overrides=None,
    ):
        if ytdl_options_presets is None:
            ytdl_options_presets = []
        normalized_entry = copy.deepcopy(entry) if isinstance(entry, dict) else entry
        already = set()
        return await self.__add_entry(
            normalized_entry,
            download_type,
            codec,
            format,
            quality,
            folder,
            custom_name_prefix,
            playlist_item_limit,
            auto_start,
            subtitle_langs or [],
            ytdl_options_presets,
            ytdl_options_overrides,
            already,
            None,
        )

    async def start_pending(self, ids):
        for id in ids:
            if self.pending.exists(id):
                dl = self.pending.get(id)
                self.pending.delete(id)
                self.queue.put(dl)
                asyncio.create_task(self.__start_download(dl))
                continue
            log.warning(f'requested start for non-existent download {id}')
        return {'status': 'ok'}

    async def cancel(self, ids):
        for id in ids:
            # Track URL so playlist add loop won't re-queue it
            self._canceled_urls.add(id)
            if self.pending.exists(id):
                self.pending.delete(id)
                await self.notifier.canceled(id)
                continue
            if not self.queue.exists(id):
                log.warning(f'requested cancel for non-existent download {id}')
                continue
            dl = self.queue.get(id)
            if dl.started():
                dl.cancel()
            else:
                dl.canceled = True
                self.queue.delete(id)
                await self.notifier.canceled(id)
        return {'status': 'ok'}

    async def clear(self, ids):
        for id in ids:
            if not self.done.exists(id):
                log.warning(f'requested delete for non-existent download {id}')
                continue
            if self.config.DELETE_FILE_ON_TRASHCAN:
                dl = self.done.get(id)
                dldirectory, calc_error = self.__calc_download_path(dl.info.download_type, dl.info.folder)
                if calc_error is not None or not dldirectory:
                    log.warning(f'deleting files for download {id} skipped: could not resolve download directory')
                else:
                    # Remove the primary output plus any per-chapter / per-subtitle
                    # outputs. Each filename is relative to the download directory.
                    rel_names = []
                    if getattr(dl.info, 'filename', None):
                        rel_names.append(dl.info.filename)
                    for extra in (getattr(dl.info, 'subtitle_files', None) or []):
                        if isinstance(extra, dict) and extra.get('filename'):
                            rel_names.append(extra['filename'])
                    for rel_name in rel_names:
                        try:
                            (Path(dldirectory) / rel_name).unlink()
                        except FileNotFoundError:
                            pass
                        except OSError as e:
                            log.warning(f'deleting file "{rel_name}" for download {id} failed with error message {e!r}')
            self.done.delete(id)
            await self.notifier.cleared(id)
        return {'status': 'ok'}

    def get(self):
        return (list((k, v.info) for k, v in self.queue.items()) +
                list((k, v.info) for k, v in self.pending.items()),
                list((k, v.info) for k, v in self.done.items()))
