#!/usr/bin/env python3
# pylint: disable=no-member,method-hidden

import os
import sys
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from aiohttp import web
from aiohttp.web import GracefulExit
from aiohttp.log import access_logger
import ssl
import socket
import logging
import json
import re
from urllib.parse import unquote

from ytdl import DownloadQueueNotifier, DownloadQueue, Download
from yt_dlp.version import __version__ as yt_dlp_version

log = logging.getLogger('main')

_NIGHTLY_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_RESTART_FOR_UPDATE = False

def _request_graceful_exit() -> None:
    raise GracefulExit()


def seconds_until_next_daily_time(time_hhmm: str, now: datetime | None = None) -> float:
    """Seconds until the next occurrence of HH:MM in local time."""
    now = now or datetime.now()
    hour, minute = map(int, time_hhmm.split(':'))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def parse_log_level(logLevel: Any) -> int | None:
    if not isinstance(logLevel, str):
        return None
    return getattr(logging, logLevel.upper(), None)

# Configure logging before Config() uses it so early messages are not dropped.
# Only configure if no handlers are set (avoid clobbering hosting app settings).
if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=parse_log_level(os.environ.get('LOGLEVEL', 'INFO')) or logging.INFO)

class Config:
    _DEFAULTS = {
        'DOWNLOAD_DIR': '.',
        'AUDIO_DOWNLOAD_DIR': '',
        'TEMP_DIR': '',
        'DOWNLOAD_DIRS_INDEXABLE': 'false',
        'DELETE_FILE_ON_TRASHCAN': 'false',
        'STATE_DIR': '.',
        'URL_PREFIX': '',
        'PUBLIC_HOST_URL': 'download/',
        'PUBLIC_HOST_AUDIO_URL': 'audio_download/',
        'OUTPUT_TEMPLATE': '%(uploader)s -- @%(extractor)s -- %(title)s -- %(upload_date>%Y-%m-%d)s.%(ext)s',
        'OUTPUT_TEMPLATE_PLAYLIST': '%(playlist_title)s/%(title)s.%(ext)s',
        'OUTPUT_TEMPLATE_CHANNEL': '%(channel)s/%(title)s.%(ext)s',
        'DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT' : '0',
        'CLEAR_COMPLETED_AFTER': '0',
        'YTDL_OPTIONS': '{}',
        'YTDL_OPTIONS_FILE': '',
        'YTDL_OPTIONS_PRESETS': '{}',
        'ALLOW_YTDL_OPTIONS_OVERRIDES': 'false',
        'CORS_ALLOWED_ORIGINS': '',
        'ROBOTS_TXT': '',
        'HOST': '0.0.0.0',
        'PORT': '8081',
        'HTTPS': 'false',
        'CERTFILE': '',
        'KEYFILE': '',
        'BASE_DIR': '',
        'DEFAULT_THEME': 'auto',
        'MAX_CONCURRENT_DOWNLOADS': '3',
        'LOGLEVEL': 'INFO',
        'ENABLE_ACCESSLOG': 'false',
        'YTDL_NIGHTLY_UPDATE_TIME': '',
    }

    _BOOLEAN = ('DOWNLOAD_DIRS_INDEXABLE', 'DELETE_FILE_ON_TRASHCAN', 'HTTPS', 'ENABLE_ACCESSLOG', 'ALLOW_YTDL_OPTIONS_OVERRIDES')

    def __init__(self):
        for k, v in self._DEFAULTS.items():
            setattr(self, k, os.environ.get(k, v))

        if not self.AUDIO_DOWNLOAD_DIR:
            self.AUDIO_DOWNLOAD_DIR = self.DOWNLOAD_DIR
        if not self.TEMP_DIR:
            self.TEMP_DIR = self.DOWNLOAD_DIR

        for k, v in self.__dict__.items():
            if k in self._BOOLEAN:
                if v not in ('true', 'false', 'True', 'False', '1', '0'):
                    log.error(f'Environment variable "{k}" is set to a non-boolean value "{v}"')
                    sys.exit(1)
                setattr(self, k, v in ('true', 'True', '1'))

        if not self.URL_PREFIX.endswith('/'):
            self.URL_PREFIX += '/'

        # A blank PUBLIC_HOST_AUDIO_URL (e.g. set empty in a compose file) bypasses the
        # default via os.environ.get, which would leave audio links root-relative and 404.
        # Fall back to the 'audio_download/' route that serves AUDIO_DOWNLOAD_DIR. When
        # PUBLIC_HOST_URL is also blank we leave it blank to preserve serving from web root.
        if not self.PUBLIC_HOST_AUDIO_URL and self.PUBLIC_HOST_URL:
            self.PUBLIC_HOST_AUDIO_URL = self._DEFAULTS['PUBLIC_HOST_AUDIO_URL']

        for attr in ('PUBLIC_HOST_URL', 'PUBLIC_HOST_AUDIO_URL'):
            val = getattr(self, attr)
            if val and not val.endswith('/'):
                setattr(self, attr, val + '/')

        # Convert relative addresses to absolute addresses to prevent the failure of file address comparison
        if self.YTDL_OPTIONS_FILE and self.YTDL_OPTIONS_FILE.startswith('.'):
            self.YTDL_OPTIONS_FILE = str(Path(self.YTDL_OPTIONS_FILE).resolve())
        if self.YTDL_NIGHTLY_UPDATE_TIME and not _NIGHTLY_TIME_RE.match(self.YTDL_NIGHTLY_UPDATE_TIME):
            log.error(
                'Environment variable "YTDL_NIGHTLY_UPDATE_TIME" must be HH:MM (24-hour), got "%s"',
                self.YTDL_NIGHTLY_UPDATE_TIME,
            )
            sys.exit(1)

        self._validate_int('MAX_CONCURRENT_DOWNLOADS', minimum=1)
        self._validate_int('PORT', minimum=1, maximum=65535)
        self._validate_int('CLEAR_COMPLETED_AFTER', minimum=0)

        self._runtime_overrides = {}

        success,_ = self.load_ytdl_options()
        if not success:
            sys.exit(1)
        success,_ = self.load_ytdl_option_presets()
        if not success:
            sys.exit(1)

    def _validate_int(self, key: str, *, minimum: int | None = None, maximum: int | None = None) -> None:
        raw = getattr(self, key)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            log.error('Environment variable "%s" must be an integer, got "%s"', key, raw)
            sys.exit(1)
        if minimum is not None and value < minimum:
            log.error('Environment variable "%s" must be >= %d, got "%s"', key, minimum, raw)
            sys.exit(1)
        if maximum is not None and value > maximum:
            log.error('Environment variable "%s" must be <= %d, got "%s"', key, maximum, raw)
            sys.exit(1)

    def set_runtime_override(self, key: str, value: Any) -> None:
        self._runtime_overrides[key] = value
        self.YTDL_OPTIONS[key] = value

    def remove_runtime_override(self, key: str) -> None:
        self._runtime_overrides.pop(key, None)
        self.YTDL_OPTIONS.pop(key, None)

    def _apply_runtime_overrides(self):
        self.YTDL_OPTIONS.update(self._runtime_overrides)

    # Keys sent to the browser. Sensitive or server-only keys (YTDL_OPTIONS,
    # paths, TLS config, etc.) are intentionally excluded.
    _FRONTEND_KEYS = (
        'PUBLIC_HOST_URL',
        'PUBLIC_HOST_AUDIO_URL',
        'DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT',
        'ALLOW_YTDL_OPTIONS_OVERRIDES',
    )

    def frontend_safe(self) -> dict[str, Any]:
        """Return only the config keys that are safe to expose to browser clients.

        Sensitive or server-only keys (YTDL_OPTIONS, file-system paths, TLS
        settings, etc.) are intentionally excluded.
        """
        return {k: getattr(self, k) for k in self._FRONTEND_KEYS}

    def load_ytdl_options(self) -> tuple[bool, str]:
        try:
            self.YTDL_OPTIONS = json.loads(os.environ.get('YTDL_OPTIONS', '{}'))
            assert isinstance(self.YTDL_OPTIONS, dict)
        except (json.decoder.JSONDecodeError, AssertionError):
            msg = 'Environment variable YTDL_OPTIONS is invalid'
            log.error(msg)
            return (False, msg)

        if not self.YTDL_OPTIONS_FILE:
            self._apply_runtime_overrides()
            return (True, '')

        log.info(f'Loading yt-dlp custom options from "{self.YTDL_OPTIONS_FILE}"')
        if not Path(self.YTDL_OPTIONS_FILE).exists():
            msg = f'File "{self.YTDL_OPTIONS_FILE}" not found'
            log.error(msg)
            return (False, msg)
        try:
            with Path(self.YTDL_OPTIONS_FILE).open() as json_data:
                opts = json.load(json_data)
            assert isinstance(opts, dict)
        except (json.decoder.JSONDecodeError, AssertionError):
            msg = 'YTDL_OPTIONS_FILE contents is invalid'
            log.error(msg)
            return (False, msg)

        self.YTDL_OPTIONS.update(opts)
        self._apply_runtime_overrides()
        return (True, '')

    def load_ytdl_option_presets(self) -> tuple[bool, str]:
        try:
            self.YTDL_OPTIONS_PRESETS = json.loads(os.environ.get('YTDL_OPTIONS_PRESETS', '{}'))
            assert isinstance(self.YTDL_OPTIONS_PRESETS, dict)
            assert all(isinstance(name, str) and isinstance(options, dict) for name, options in self.YTDL_OPTIONS_PRESETS.items())
        except (json.decoder.JSONDecodeError, AssertionError):
            msg = 'Environment variable YTDL_OPTIONS_PRESETS is invalid'
            log.error(msg)
            return (False, msg)

        return (True, '')

config = Config()
# Align root logger level with Config (keeps a single source of truth).
# This re-applies the log level after Config loads, in case LOGLEVEL was
# overridden by config file settings or differs from the environment variable.
logging.getLogger().setLevel(parse_log_level(str(config.LOGLEVEL)) or logging.INFO)

class ObjectSerializer(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        # Prefer an explicit client-facing view when the object provides one
        # (e.g. DownloadInfo / SubscriptionInfo) so server-only or bulky fields
        # are never broadcast to browser clients.
        to_public = getattr(obj, 'to_public_dict', None)
        if callable(to_public):
            return to_public()
        # Fall back to __dict__ for other custom objects
        if hasattr(obj, '__dict__'):
            return obj.__dict__
        # Convert iterables (generators, dict_items, etc.) to lists
        # Exclude strings and bytes which are also iterable
        elif hasattr(obj, '__iter__') and not isinstance(obj, (str, bytes)):
            try:
                return list(obj)
            except Exception:
                pass
        # Fall back to default behavior
        return json.JSONEncoder.default(self, obj)

serializer = ObjectSerializer()

_STATE_DIR_REAL = Path(config.STATE_DIR).resolve()


def _is_within_state_dir(target: str | Path) -> bool:
    return Path(target).is_relative_to(_STATE_DIR_REAL)


@web.middleware
async def state_dir_guard(request: web.Request, handler: Any) -> web.StreamResponse:
    for prefix, base in (
        (config.URL_PREFIX + 'download/', config.DOWNLOAD_DIR),
        (config.URL_PREFIX + 'audio_download/', config.AUDIO_DOWNLOAD_DIR),
    ):
        if request.path.startswith(prefix):
            rel = unquote(request.path[len(prefix):])
            target = (Path(base) / rel).resolve()
            if _is_within_state_dir(target):
                raise web.HTTPNotFound()
            break
    return await handler(request)


app = web.Application(middlewares=[state_dir_guard])
_cors_origins = [o.strip() for o in config.CORS_ALLOWED_ORIGINS.split(',') if o.strip()] if config.CORS_ALLOWED_ORIGINS else []
routes = web.RouteTableDef()
VALID_DOWNLOAD_TYPES = {'video', 'audio', 'captions', 'thumbnail'}
VALID_VIDEO_CODECS = {'auto', 'h264', 'h265', 'av1', 'vp9'}
VALID_VIDEO_FORMATS = {'any', 'mp4', 'ios'}
VALID_AUDIO_FORMATS = {'m4a', 'mp3', 'opus', 'wav', 'flac'}
VALID_THUMBNAIL_FORMATS = {'jpg'}
def _parse_ytdl_options_overrides(value: Any, *, enabled: bool) -> dict[str, Any]:
    if value is None or value == '':
        return {}

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(reason='ytdl_options_overrides must be valid JSON') from exc

    if not isinstance(value, dict):
        raise web.HTTPBadRequest(reason='ytdl_options_overrides must be a JSON object')

    if value and not enabled:
        raise web.HTTPBadRequest(reason='ytdl_options_overrides are disabled')

    return value


def _parse_ytdl_options_presets(post: dict) -> list[str]:
    """Normalize preset names from add/subscribe body; supports list or legacy singular string."""
    raw = post.get('ytdl_options_presets')
    if raw is None:
        raw = post.get('ytdl_options_preset')
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    raise web.HTTPBadRequest(
        reason='ytdl_options_presets must be a JSON array of strings (or legacy ytdl_options_preset string)',
    )


class Notifier(DownloadQueueNotifier):
    async def added(self, dl):
        log.info(f"Notifier: Download added - {dl.title}")

    async def updated(self, dl):
        log.debug(f"Notifier: Download updated - {dl.title}")

    async def completed(self, dl):
        log.info(f"Notifier: Download completed - {dl.title}")

    async def canceled(self, id):
        log.info(f"Notifier: Download canceled - {id}")

    async def cleared(self, id):
        log.info(f"Notifier: Download cleared - {id}")

dqueue = DownloadQueue(config, Notifier())


async def _download_queue_startup(app):
    await dqueue.initialize()


async def _shutdown_download_manager(app):
    Download.shutdown_manager()


app.on_startup.append(_download_queue_startup)
app.on_cleanup.append(_shutdown_download_manager)


async def _schedule_nightly_update() -> None:
    global _RESTART_FOR_UPDATE
    time_hhmm = config.YTDL_NIGHTLY_UPDATE_TIME
    if not time_hhmm:
        return
    delay = seconds_until_next_daily_time(time_hhmm)
    log.info('Next yt-dlp nightly update in %.0f seconds (at %s local time)', delay, time_hhmm)
    await asyncio.sleep(delay)
    log.info('Scheduled yt-dlp nightly update: requesting restart')
    _RESTART_FOR_UPDATE = True
    asyncio.get_running_loop().call_soon(_request_graceful_exit)


async def _start_nightly_update_schedule(app):
    asyncio.create_task(_schedule_nightly_update())


app.on_startup.append(_start_nightly_update_schedule)


async def _read_json_request(request: web.Request) -> dict:
    try:
        post = await request.json()
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(reason='Invalid JSON request body') from exc
    if not isinstance(post, dict):
        raise web.HTTPBadRequest(reason='JSON request body must be an object')
    return post


def parse_download_options(post: dict[str, Any]) -> dict[str, Any]:
    """Validate add body; raise HTTPBadRequest on invalid input."""
    url = post.get('url')
    download_type = post.get('download_type')
    codec = post.get('codec')
    format = post.get('format')
    quality = post.get('quality')
    if not url or not quality or not download_type:
        raise web.HTTPBadRequest(reason="missing 'url', 'download_type', or 'quality'")
    url = str(url).strip()
    folder = post.get('folder')
    custom_name_prefix = post.get('custom_name_prefix')
    playlist_item_limit = post.get('playlist_item_limit')
    auto_start = post.get('auto_start')
    ytdl_options_overrides = post.get('ytdl_options_overrides')

    if custom_name_prefix is None:
        custom_name_prefix = ''
    if custom_name_prefix and ('..' in custom_name_prefix or custom_name_prefix.startswith('/') or custom_name_prefix.startswith('\\')):
        raise web.HTTPBadRequest(reason='custom_name_prefix must not contain ".." or start with a path separator')
    if auto_start is None:
        auto_start = True
    if playlist_item_limit is None:
        playlist_item_limit = config.DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT
    download_type = str(download_type).strip().lower()
    codec = str(codec or 'auto').strip().lower()
    format = str(format or '').strip().lower()
    quality = str(quality).strip().lower()
    raw_langs = post.get("subtitle_langs") or []
    if isinstance(raw_langs, str):
        raw_langs = [l.strip() for l in raw_langs.split(",") if l.strip()]
    subtitle_langs = []
    for code in raw_langs:
        code = str(code).strip()
        if not re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$', code):
            raise web.HTTPBadRequest(reason=f'Invalid subtitle language code: {code!r}')
        subtitle_langs.append(code)

    ytdl_options_presets = _parse_ytdl_options_presets(post)
    ytdl_options_overrides = _parse_ytdl_options_overrides(
        ytdl_options_overrides,
        enabled=config.ALLOW_YTDL_OPTIONS_OVERRIDES,
    )

    for preset_name in ytdl_options_presets:
        if preset_name not in config.YTDL_OPTIONS_PRESETS:
            raise web.HTTPBadRequest(reason='ytdl_options_presets must only contain configured preset names')

    if download_type not in VALID_DOWNLOAD_TYPES:
        raise web.HTTPBadRequest(reason=f'download_type must be one of {sorted(VALID_DOWNLOAD_TYPES)}')
    if codec not in VALID_VIDEO_CODECS:
        raise web.HTTPBadRequest(reason=f'codec must be one of {sorted(VALID_VIDEO_CODECS)}')

    if download_type == 'video':
        if format not in VALID_VIDEO_FORMATS:
            raise web.HTTPBadRequest(reason=f'format must be one of {sorted(VALID_VIDEO_FORMATS)} for video')
        if quality not in {'best', 'worst', '2160', '1440', '1080', '720', '480', '360', '240'}:
            raise web.HTTPBadRequest(reason="quality must be one of ['best', '2160', '1440', '1080', '720', '480', '360', '240', 'worst'] for video")
    elif download_type == 'audio':
        if format not in VALID_AUDIO_FORMATS:
            raise web.HTTPBadRequest(reason=f'format must be one of {sorted(VALID_AUDIO_FORMATS)} for audio')
        allowed_audio_qualities = {'best'}
        if format == 'mp3':
            allowed_audio_qualities |= {'320', '192', '128'}
        elif format == 'm4a':
            allowed_audio_qualities |= {'192', '128'}
        if quality not in allowed_audio_qualities:
            raise web.HTTPBadRequest(reason=f'quality must be one of {sorted(allowed_audio_qualities)} for format {format}')
        codec = 'auto'
    elif download_type in ('captions', 'thumbnail'):
        quality = 'best'
        codec = 'auto'

    try:
        playlist_item_limit = int(playlist_item_limit)
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason='playlist_item_limit must be an integer') from exc

    return {
        'url': url,
        'download_type': download_type,
        'codec': codec,
        'format': format,
        'quality': quality,
        'folder': folder,
        'custom_name_prefix': custom_name_prefix,
        'playlist_item_limit': playlist_item_limit,
        'auto_start': auto_start,
        'subtitle_langs': subtitle_langs,
        'ytdl_options_presets': ytdl_options_presets,
        'ytdl_options_overrides': ytdl_options_overrides,
    }


@routes.post(config.URL_PREFIX + 'add')
async def add(request: web.Request) -> web.Response:
    log.info("Received request to add download")
    post = await _read_json_request(request)
    try:
        o = parse_download_options(post)
    except web.HTTPBadRequest as e:
        log.error("Bad request: %s", e.reason)
        raise
    log.info(
        "Add download request: type=%s quality=%s format=%s has_folder=%s auto_start=%s",
        o['download_type'],
        o['quality'],
        o['format'],
        bool(o.get('folder')),
        o['auto_start'],
    )
    status = await dqueue.add(
        o['url'],
        o['download_type'],
        o['codec'],
        o['format'],
        o['quality'],
        o['folder'],
        o['custom_name_prefix'],
        o['playlist_item_limit'],
        o['auto_start'],
        subtitle_langs=o['subtitle_langs'],
        ytdl_options_presets=o['ytdl_options_presets'],
        ytdl_options_overrides=o['ytdl_options_overrides'],
    )
    return web.Response(text=serializer.encode(status))


@routes.get(config.URL_PREFIX + 'presets')
async def presets(request: web.Request) -> web.Response:
    return web.Response(
        text=serializer.encode({'presets': sorted(config.YTDL_OPTIONS_PRESETS.keys())}),
        content_type='application/json',
    )

@routes.post(config.URL_PREFIX + 'cancel-add')
async def cancel_add(request: web.Request) -> web.Response:
    dqueue.cancel_add()
    return web.Response(text=serializer.encode({'status': 'ok'}), content_type='application/json')


@routes.post(config.URL_PREFIX + 'delete')
async def delete(request: web.Request) -> web.Response:
    post = await _read_json_request(request)
    ids = post.get('ids')
    where = post.get('where')
    if not ids or where not in ['queue', 'done']:
        log.error("Bad request: missing 'ids' or incorrect 'where' value")
        raise web.HTTPBadRequest()
    status = await (dqueue.cancel(ids) if where == 'queue' else dqueue.clear(ids))
    log.info(f"Download delete request processed for ids: {ids}, where: {where}")
    return web.Response(text=serializer.encode(status))

@routes.post(config.URL_PREFIX + 'start')
async def start(request: web.Request) -> web.Response:
    post = await _read_json_request(request)
    ids = post.get('ids')
    log.info(f"Received request to start pending downloads for ids: {ids}")
    status = await dqueue.start_pending(ids)
    return web.Response(text=serializer.encode(status))


COOKIES_PATH = Path(config.STATE_DIR) / 'cookies.txt'

@routes.post(config.URL_PREFIX + 'upload-cookies')
async def upload_cookies(request: web.Request) -> web.Response:
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != 'cookies':
        return web.Response(status=400, text=serializer.encode({'status': 'error', 'msg': 'No cookies file provided'}))

    max_size = 1_000_000  # 1MB limit
    size = 0
    content = bytearray()
    while True:
        chunk = await field.read_chunk()
        if not chunk:
            break
        size += len(chunk)
        if size > max_size:
            return web.Response(status=400, text=serializer.encode({'status': 'error', 'msg': 'Cookie file too large (max 1MB)'}))
        content.extend(chunk)

    tmp_cookie_path = COOKIES_PATH.with_name(COOKIES_PATH.name + '.tmp')
    tmp_cookie_path.write_bytes(bytes(content))
    # Cookies are sensitive auth material; restrict to owner read/write only
    # (the container's default umask would otherwise leave them group/world readable).
    try:
        tmp_cookie_path.chmod(0o600)
    except OSError as exc:
        log.warning(f'Could not restrict permissions on cookies file: {exc}')
    tmp_cookie_path.replace(COOKIES_PATH)
    config.set_runtime_override('cookiefile', str(COOKIES_PATH))
    log.info(f'Cookies file uploaded ({size} bytes)')
    return web.Response(text=serializer.encode({'status': 'ok', 'msg': f'Cookies uploaded ({size} bytes)'}))

@routes.post(config.URL_PREFIX + 'delete-cookies')
async def delete_cookies(request: web.Request) -> web.Response:
    has_uploaded_cookies = COOKIES_PATH.exists()
    configured_cookiefile = config.YTDL_OPTIONS.get('cookiefile')
    has_manual_cookiefile = isinstance(configured_cookiefile, str) and configured_cookiefile and configured_cookiefile != str(COOKIES_PATH)

    if not has_uploaded_cookies:
        if has_manual_cookiefile:
            return web.Response(
                status=400,
                text=serializer.encode({
                    'status': 'error',
                    'msg': 'Cookies are configured manually via YTDL_OPTIONS (cookiefile). Remove or change that setting manually; UI delete only removes uploaded cookies.'
                })
            )
        return web.Response(status=400, text=serializer.encode({'status': 'error', 'msg': 'No uploaded cookies to delete'}))

    COOKIES_PATH.unlink()
    config.remove_runtime_override('cookiefile')
    success, msg = config.load_ytdl_options()
    if not success:
        log.error(f'Cookies file deleted, but failed to reload YTDL_OPTIONS: {msg}')
        return web.Response(status=500, text=serializer.encode({'status': 'error', 'msg': f'Cookies file deleted, but failed to reload YTDL_OPTIONS: {msg}'}))

    log.info('Cookies file deleted')
    return web.Response(text=serializer.encode({'status': 'ok'}))

@routes.get(config.URL_PREFIX + 'cookie-status')
async def cookie_status(request: web.Request) -> web.Response:
    configured_cookiefile = config.YTDL_OPTIONS.get('cookiefile')
    has_configured_cookies = isinstance(configured_cookiefile, str) and Path(configured_cookiefile).exists()
    has_uploaded_cookies = COOKIES_PATH.exists()
    exists = has_uploaded_cookies or has_configured_cookies
    return web.Response(text=serializer.encode({'status': 'ok', 'has_cookies': exists}))

@routes.get(config.URL_PREFIX + 'history')
async def history(request: web.Request) -> web.Response:
    history = { 'done': [], 'queue': [], 'pending': []}

    for _, v in dqueue.queue.saved_items():
        history['queue'].append(v)
    for _, v in dqueue.done.saved_items():
        history['done'].append(v)
    for _, v in dqueue.pending.saved_items():
        history['pending'].append(v)

    log.info("Sending download history")
    return web.Response(text=serializer.encode(history))

@routes.get(config.URL_PREFIX + 'queue')
async def queue_state(request: web.Request) -> web.Response:
    state = dqueue.get()
    return web.Response(text=serializer.encode(state), content_type='application/json')

@routes.get(config.URL_PREFIX + 'logs')
async def get_logs(request: web.Request) -> web.Response:
    dl_id = request.query.get('id')
    if not dl_id:
        raise web.HTTPBadRequest(reason='missing id')
    dl = (
        dqueue.queue.dict.get(dl_id)
        or dqueue.done.dict.get(dl_id)
        or dqueue.pending.dict.get(dl_id)
    )
    lines = dl.info.logs if dl is not None else []
    return web.Response(text=serializer.encode(lines), content_type='application/json')

@routes.get(config.URL_PREFIX + 'configuration')
async def configuration(request: web.Request) -> web.Response:
    return web.Response(text=serializer.encode(config.frontend_safe()), content_type='application/json')

@routes.get(config.URL_PREFIX)
async def index(request: web.Request) -> web.Response:
    response = web.FileResponse(Path(config.BASE_DIR) / 'ui/index.html')
    if 'metube_theme' not in request.cookies:
        response.set_cookie('metube_theme', config.DEFAULT_THEME)
    return response

@routes.get(config.URL_PREFIX + 'robots.txt')
async def robots(request: web.Request) -> web.Response:
    if config.ROBOTS_TXT:
        response = web.FileResponse(Path(config.BASE_DIR) / config.ROBOTS_TXT)
    else:
        response = web.Response(
            text="User-agent: *\nDisallow: /download/\nDisallow: /audio_download/\n"
        )
    return response

@routes.get(config.URL_PREFIX + 'version')
async def version(request: web.Request) -> web.Response:
    return web.json_response({
        "yt-dlp": yt_dlp_version,
        "version": os.getenv("METUBE_VERSION", "dev")
    })

if config.URL_PREFIX != '/':
    @routes.get('/')
    async def index_redirect_root(request):
        return web.HTTPFound(config.URL_PREFIX)

    @routes.get(config.URL_PREFIX[:-1])
    async def index_redirect_dir(request):
        return web.HTTPFound(config.URL_PREFIX)

routes.static(config.URL_PREFIX + 'download/', config.DOWNLOAD_DIR, show_index=config.DOWNLOAD_DIRS_INDEXABLE)
routes.static(config.URL_PREFIX + 'audio_download/', config.AUDIO_DOWNLOAD_DIR, show_index=config.DOWNLOAD_DIRS_INDEXABLE)
routes.static(config.URL_PREFIX, Path(config.BASE_DIR) / 'ui')
try:
    app.add_routes(routes)
except ValueError as e:
    if 'ui/index.html' in str(e) or 'ui' in str(e):
        raise RuntimeError('Could not find the frontend UI static assets. Expected ui/index.html') from e
    raise e

# https://github.com/aio-libs/aiohttp/pull/4615 waiting for release
# @routes.options(config.URL_PREFIX + 'add')
async def add_cors(request):
    return web.Response(text=serializer.encode({"status": "ok"}))

app.router.add_route('OPTIONS', config.URL_PREFIX + 'add', add_cors)
app.router.add_route('OPTIONS', config.URL_PREFIX + 'cancel-add', add_cors)
app.router.add_route('OPTIONS', config.URL_PREFIX + 'upload-cookies', add_cors)
app.router.add_route('OPTIONS', config.URL_PREFIX + 'delete-cookies', add_cors)
app.router.add_route('OPTIONS', config.URL_PREFIX + 'logs', add_cors)

async def on_prepare(request: web.Request, response: web.StreamResponse) -> None:
    origin = request.headers.get('Origin')
    if origin and _cors_origins and ('*' in _cors_origins or origin in _cors_origins):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'

app.on_response_prepare.append(on_prepare)

def supports_reuse_port() -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.close()
        return True
    except (AttributeError, OSError):
        return False

def is_access_log_enabled() -> logging.Logger | None:
    if config.ENABLE_ACCESSLOG:
        return access_logger
    return None

if __name__ == '__main__':
    logging.getLogger().setLevel(parse_log_level(config.LOGLEVEL) or logging.INFO)
    log.info(f"Listening on {config.HOST}:{config.PORT}")


    # Auto-detect cookie file on startup
    if COOKIES_PATH.exists():
        config.set_runtime_override('cookiefile', str(COOKIES_PATH))
        log.info(f'Cookie file detected at {COOKIES_PATH}')

    if config.HTTPS:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(certfile=config.CERTFILE, keyfile=config.KEYFILE)
        web.run_app(app, host=config.HOST, port=int(config.PORT), reuse_port=supports_reuse_port(), ssl_context=ssl_context, access_log=is_access_log_enabled())
    else:
        web.run_app(app, host=config.HOST, port=int(config.PORT), reuse_port=supports_reuse_port(), access_log=is_access_log_enabled())
    if _RESTART_FOR_UPDATE:
        sys.exit(42)
