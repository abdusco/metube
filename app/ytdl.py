import os
import yt_dlp
import collections
import collections.abc
import copy
import pickle
from collections import OrderedDict
from pathlib import Path
import time
import threading
import multiprocessing
import logging
import re
import types
from typing import Any, Optional

import yt_dlp.networking.impersonate
from yt_dlp.utils import STR_FORMAT_RE_TMPL, STR_FORMAT_TYPES
from dl_formats import get_format, get_opts
from state_store import AtomicJsonStore, from_json_compatible, to_json_compatible


def _entry_id(entry: dict) -> Optional[str]:
    eid = entry.get("id")
    if eid is not None:
        return str(eid)
    return entry.get("webpage_url") or entry.get("url")

log = logging.getLogger('ytdl')

_WINDOWS_INVALID_PATH_CHARS = re.compile(r'[\\:*?"<>|]')


def _sanitize_path_component(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _WINDOWS_INVALID_PATH_CHARS.sub('_', value)


_OUTTMPL_FIELD_RE = re.compile(
    STR_FORMAT_RE_TMPL.format('[^)]+', f'[{STR_FORMAT_TYPES}ljhqBUDS]')
)


def _resolve_outtmpl_fields(template: str, info_dict: dict, prefixes: tuple[str, ...]) -> str:
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


def _sanitize_entry_for_pickle(obj: Any, _depth: int = 0) -> Any:
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
    def added(self, dl: "DownloadInfo") -> None:
        raise NotImplementedError

    def updated(self, dl: "DownloadInfo") -> None:
        raise NotImplementedError

    def completed(self, dl: "DownloadInfo") -> None:
        raise NotImplementedError

    def canceled(self, id: str) -> None:
        raise NotImplementedError

    def cleared(self, id: str) -> None:
        raise NotImplementedError

class DownloadInfo:
    def __init__(
        self,
        id: str,
        title: str,
        url: str,
        quality: str,
        download_type: str,
        codec: str,
        format: str,
        folder: str | None,
        error: str | None,
        entry: dict[str, Any] | None,
        subtitle_langs: list[str] | None = None,
        ytdl_options_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.id = id
        self.title = title
        self.url = url
        self.quality = quality
        self.download_type = download_type
        self.codec = codec
        self.format = format
        self.folder = folder
        self.msg = self.percent = self.speed = self.eta = None
        self.status = "pending"
        self.size = None
        self.timestamp = time.time_ns()
        self.error = error
        self.entry = _sanitize_entry_for_pickle(entry) if entry is not None else None
        self.subtitle_langs = list(subtitle_langs) if subtitle_langs else []
        self.ytdl_options_overrides = dict(ytdl_options_overrides or {})
        self.subtitle_files = []
        self.logs: list = []

    _PUBLIC_EXCLUDED_FIELDS = ("entry", "logs")

    def to_public_dict(self) -> dict:
        return {
            k: v
            for k, v in self.__dict__.items()
            if k not in self._PUBLIC_EXCLUDED_FIELDS
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        if not getattr(self, "codec", None):
            self.codec = "auto"
        if not hasattr(self, "folder"):
            self.folder = ""
        if not hasattr(self, "subtitle_langs"):
            self.subtitle_langs = []
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
    "subtitle_langs",
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

    def __init__(
        self,
        download_dir: Path | None,
        temp_dir: Path | None,
        output_template: str | None,
        quality: str,
        format: str,
        ytdl_opts: dict[str, Any],
        info: DownloadInfo,
    ) -> None:
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
        self.notifier = None
        self._status_thread = None

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
                    requested_subtitles = d.get('info_dict', {}).get('requested_subtitles', {}) or {}
                    for subtitle in requested_subtitles.values():
                        if isinstance(subtitle, dict) and subtitle.get('filepath'):
                            self.status_queue.put({'subtitle_file': subtitle['filepath']})

            _sq = self.status_queue
            class _YtdlLogger:
                def debug(self, msg):
                    if msg.startswith('[debug] ') and not debug_logging:
                        return
                    _sq.put({'log': msg})
                def info(self, msg):
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
                'paths': {"home": str(self.download_dir), "temp": str(self.temp_dir)},
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

    def start(self, notifier: DownloadQueueNotifier) -> None:
        log.info(f"Preparing download for: {self.info.title}")
        if Download.manager is None:
            Download.manager = multiprocessing.Manager()
        self.status_queue = Download.manager.Queue()
        self.proc = multiprocessing.Process(target=self._download)
        self.proc.start()
        self.notifier = notifier
        self.info.status = 'preparing'
        self.notifier.updated(self.info)
        self._status_thread = threading.Thread(target=self._poll_status, daemon=True)
        self._status_thread.start()
        self.proc.join()
        if self.status_queue is not None:
            self.status_queue.put(None)
        if self._status_thread is not None:
            self._status_thread.join()

    def cancel(self) -> None:
        log.info(f"Cancelling download: {self.info.title}")
        if self.running():
            try:
                self.proc.kill()
            except Exception as e:
                log.error(f"Error killing process for {self.info.title}: {e}")
        self.canceled = True
        if self.status_queue is not None:
            self.status_queue.put(None)

    def close(self) -> None:
        log.info(f"Closing download process for: {self.info.title}")
        if self.started():
            self.proc.close()

    def running(self) -> bool:
        try:
            return self.proc is not None and self.proc.is_alive()
        except ValueError:
            return False

    def started(self) -> bool:
        return self.proc is not None

    def _poll_status(self) -> None:
        while True:
            status = self.status_queue.get()
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
                self.info.filename = rel_name
                _fn = Path(fileName)
                self.info.size = _fn.stat().st_size if _fn.exists() else None

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
            self.notifier.updated(self.info)

class PersistentQueue:
    def __init__(self, name: str, path: Path) -> None:
        self.identifier = name
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.with_suffix('.json')
        self.store = AtomicJsonStore(self.path, kind=f"persistent_queue:{name}")
        self.dict: OrderedDict[str, Download] = OrderedDict()

    def load(self) -> None:
        for k, v in self.saved_items():
            self.dict[k] = Download(
                download_dir=None,
                temp_dir=None,
                output_template=None,
                quality=getattr(v, 'quality', 'best'),
                format=getattr(v, 'format', 'any'),
                ytdl_opts={},
                info=v,
            )

    def exists(self, key: str) -> bool:
        return key in self.dict

    def get(self, key: str) -> Download:
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

    def put(self, value: Download) -> None:
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

    def delete(self, key: str) -> None:
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

    def empty(self) -> bool:
        return not bool(self.dict)

class DownloadQueue:
    def __init__(self, config: Any, notifier: DownloadQueueNotifier) -> None:
        self.config = config
        self.notifier = notifier
        _state = Path(self.config.STATE_DIR)
        self.queue = PersistentQueue("queue", _state / 'queue')
        self.done = PersistentQueue("completed", _state / 'completed')
        self.active_downloads = set()
        self.semaphore = threading.Semaphore(int(self.config.MAX_CONCURRENT_DOWNLOADS))
        self._lock = threading.Lock()
        self.done.load()

    def initialize(self):
        log.info("Initializing DownloadQueue")
        for _, v in self.queue.saved_items():
            self.__add_download(v)

    def _run_download(self, download):
        if download.canceled:
            log.info(f"Download {download.info.title} was canceled, skipping start.")
            return
        with self.semaphore:
            if download.canceled:
                log.info(f"Download {download.info.title} was canceled, skipping start.")
                return
            download.start(self.notifier)
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
        with self._lock:
            if self.queue.exists(download.info.url):
                self.queue.delete(download.info.url)
                if download.canceled:
                    self.notifier.canceled(download.info.url)
                else:
                    self.done.put(download)
                    self.notifier.completed(download.info)
                    clear_after = self.config.CLEAR_COMPLETED_AFTER
                    if clear_after > 0:
                        threading.Thread(
                            target=self.__auto_clear_after_delay,
                            args=(download.info.url, clear_after),
                            daemon=True,
                        ).start()

    def __auto_clear_after_delay(self, url, delay_seconds):
        time.sleep(delay_seconds)
        if self.done.exists(url):
            log.debug(f'Auto-clearing completed download: {url}')
            self.clear([url])

    def _build_ytdl_options(self, ytdl_options_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        opts = dict(self.config.YTDL_OPTIONS)
        opts.update(ytdl_options_overrides or {})
        return opts

    def __extract_info(self, url: str, ytdl_options_overrides: dict[str, Any] | None = None) -> Any:
        debug_logging = logging.getLogger().isEnabledFor(logging.DEBUG)
        user_opts = self._build_ytdl_options(ytdl_options_overrides)
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

    def __calc_download_path(self, download_type: str, folder: str | None) -> tuple[Path | None, dict[str, Any] | None]:
        base_directory = self.config.AUDIO_DOWNLOAD_DIR if download_type == 'audio' else self.config.DOWNLOAD_DIR
        if folder:
            real_base = Path(base_directory).resolve()
            dldirectory = (Path(base_directory) / folder).resolve()
            if not dldirectory.is_relative_to(real_base):
                return None, {'status': 'error', 'msg': f'Folder "{folder}" must resolve inside the base download directory "{real_base}"'}
            dldirectory.mkdir(parents=True, exist_ok=True)
            return dldirectory, None
        return Path(base_directory), None

    def __add_download(self, dl):
        dldirectory, error_message = self.__calc_download_path(dl.download_type, dl.folder)
        if error_message is not None:
            return error_message
        output = self.config.OUTPUT_TEMPLATE
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
        ytdl_options = self._build_ytdl_options(getattr(dl, 'ytdl_options_overrides', {}) or {})
        download = Download(
            download_dir=dldirectory,
            temp_dir=Path(self.config.TEMP_DIR),
            output_template=output,
            quality=dl.quality,
            format=dl.format,
            ytdl_opts=ytdl_options,
            info=dl,
        )
        with self._lock:
            self.queue.put(download)
        threading.Thread(target=self._run_download, args=(download,), daemon=True).start()
        self.notifier.added(dl)

    def __add_entry(
        self,
        entry: dict[str, Any] | None,
        download_type: str,
        codec: str,
        format: str,
        quality: str,
        folder: str | None,
        subtitle_langs: list[str],
        ytdl_options_overrides: dict[str, Any],
        already: set[str],
    ) -> dict[str, Any]:
        if not entry:
            return {'status': 'error', 'msg': "Invalid/empty data was given."}

        error = entry.get("msg") if entry else None
        etype = entry.get('_type') or 'video'

        if etype.startswith('url'):
            log.debug('Processing as a url')
            return self.add(
                url=entry['url'],
                download_type=download_type,
                codec=codec,
                format=format,
                quality=quality,
                folder=folder,
                subtitle_langs=subtitle_langs,
                ytdl_options_overrides=ytdl_options_overrides,
                already=already,
            )
        elif etype == 'playlist' or etype == 'channel':
            log.debug(f'Processing as a {etype}')
            entries = entry['entries']
            if isinstance(entries, types.GeneratorType):
                entries = list(entries)
            total_entries = len(entries)
            log.info(f'{etype} detected with {total_entries} entries')
            index_digits = len(str(total_entries))
            results = []
            for index, etr in enumerate(entries, start=1):
                if "id" not in etr:
                    etr["id"] = _entry_id(etr)
                etr["_type"] = "video"
                etr[etype] = entry.get("id") or entry.get("channel_id") or entry.get("channel")
                etr[f"{etype}_index"] = '{{0:0{0:d}d}}'.format(index_digits).format(index)
                etr[f"{etype}_count"] = total_entries
                etr[f"{etype}_autonumber"] = index
                etr["n_entries"] = total_entries
                etr["__last_playlist_index"] = total_entries
                for property in ("id", "title", "uploader", "uploader_id"):
                    if property in entry:
                        etr[f"{etype}_{property}"] = entry[property]
                results.append(
                    self.__add_entry(
                        entry=etr,
                        download_type=download_type,
                        codec=codec,
                        format=format,
                        quality=quality,
                        folder=folder,
                        subtitle_langs=subtitle_langs,
                        ytdl_options_overrides=ytdl_options_overrides,
                        already=already,
                    )
                )
            if any(res['status'] == 'error' for res in results):
                return {'status': 'error', 'msg': ', '.join(res['msg'] for res in results if res['status'] == 'error' and 'msg' in res)}
            return {'status': 'ok'}
        elif etype == 'video' or (etype.startswith('url') and 'id' in entry and 'title' in entry):
            log.debug('Processing as a video')
            key = entry.get('webpage_url') or entry['url']
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
                    error=error,
                    entry=entry,
                    subtitle_langs=subtitle_langs,
                    ytdl_options_overrides=ytdl_options_overrides,
                )
                self.__add_download(dl)
            return {'status': 'ok'}
        return {'status': 'error', 'msg': f'Unsupported resource "{etype}"'}

    def add(
        self,
        url: str,
        download_type: str,
        codec: str,
        format: str,
        quality: str,
        folder: str | None,
        subtitle_langs: list[str] | None = None,
        ytdl_options_overrides: dict[str, Any] | None = None,
        already: set[str] | None = None,
    ) -> dict[str, Any]:
        log.info(
            f'adding {url}: {download_type=} {codec=} {format=} {quality=} {already=} {folder=} {subtitle_langs=}'
        )
        already = set() if already is None else already
        if url in already:
            log.info('recursion detected, skipping')
            return {'status': 'ok'}
        already.add(url)
        try:
            entry = self.__extract_info(url, ytdl_options_overrides)
        except yt_dlp.utils.YoutubeDLError as exc:
            return {'status': 'error', 'msg': str(exc)}
        return self.__add_entry(
            entry=entry,
            download_type=download_type,
            codec=codec,
            format=format,
            quality=quality,
            folder=folder,
            subtitle_langs=subtitle_langs or [],
            ytdl_options_overrides=ytdl_options_overrides or {},
            already=already,
        )

    def add_entry(
        self,
        entry: dict[str, Any],
        download_type: str,
        codec: str,
        format: str,
        quality: str,
        folder: str | None,
        subtitle_langs: list[str] | None = None,
        ytdl_options_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_entry = copy.deepcopy(entry) if isinstance(entry, dict) else entry
        already = set()
        return self.__add_entry(
            entry=normalized_entry,
            download_type=download_type,
            codec=codec,
            format=format,
            quality=quality,
            folder=folder,
            subtitle_langs=subtitle_langs or [],
            ytdl_options_overrides=ytdl_options_overrides or {},
            already=already,
        )

    def cancel(self, ids: list[str]) -> dict[str, Any]:
        for id in ids:
            with self._lock:
                if not self.queue.exists(id):
                    log.warning(f'requested cancel for non-existent download {id}')
                    continue
                dl = self.queue.get(id)
                if dl.started():
                    dl.cancel()
                else:
                    dl.canceled = True
                    self.queue.delete(id)
                    self.notifier.canceled(id)
        return {'status': 'ok'}

    def clear(self, ids: list[str]) -> dict[str, Any]:
        for id in ids:
            with self._lock:
                if not self.done.exists(id):
                    log.warning(f'requested delete for non-existent download {id}')
                    continue
                if self.config.DELETE_FILE_ON_TRASHCAN:
                    dl = self.done.get(id)
                    dldirectory, calc_error = self.__calc_download_path(dl.info.download_type, dl.info.folder)
                    if calc_error is not None or not dldirectory:
                        log.warning(f'deleting files for download {id} skipped: could not resolve download directory')
                    else:
                        rel_names = []
                        if getattr(dl.info, 'filename', None):
                            rel_names.append(dl.info.filename)
                        for extra in (getattr(dl.info, 'subtitle_files', None) or []):
                            if isinstance(extra, dict) and extra.get('filename'):
                                rel_names.append(extra['filename'])
                        for rel_name in rel_names:
                            try:
                                (dldirectory / rel_name).unlink()
                            except FileNotFoundError:
                                pass
                            except OSError as e:
                                log.warning(f'deleting file "{rel_name}" for download {id} failed with error message {e!r}')
                self.done.delete(id)
                self.notifier.cleared(id)
        return {'status': 'ok'}

    def get(self) -> tuple[list, list]:
        with self._lock:
            return (
                list((k, v.info) for k, v in self.queue.items()),
                list((k, v.info) for k, v in self.done.items()),
            )
