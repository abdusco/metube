#!/usr/bin/env python3
# pylint: disable=no-member,method-hidden

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import unquote

from aiohttp import web
from aiohttp.web import GracefulExit
from pydantic import BaseModel, Field, PrivateAttr, ValidationError as PydanticValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError

from ytdl import DownloadQueueNotifier, DownloadQueue, Download

log = logging.getLogger('main')

_NIGHTLY_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_SUBTITLE_LANG_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$')
_RESTART_FOR_UPDATE = False

VALID_DOWNLOAD_TYPES = frozenset({'video', 'audio', 'captions', 'thumbnail'})
VALID_VIDEO_CODECS = frozenset({'auto', 'h264', 'h265', 'av1', 'vp9'})
VALID_VIDEO_FORMATS = frozenset({'any', 'mp4', 'ios'})
VALID_AUDIO_FORMATS = frozenset({'m4a', 'mp3', 'opus', 'wav', 'flac'})
VALID_VIDEO_QUALITIES = frozenset({'best', 'worst', '2160', '1440', '1080', '720', '480', '360', '240'})


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


if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=parse_log_level(os.environ.get('LOGLEVEL', 'INFO')) or logging.INFO)


class Config(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True, extra='ignore')

    DOWNLOAD_DIR: str = '.'
    AUDIO_DOWNLOAD_DIR: str = ''
    TEMP_DIR: str = ''
    DELETE_FILE_ON_TRASHCAN: bool = False
    STATE_DIR: str = '.'
    PUBLIC_HOST_URL: str = 'download/'
    PUBLIC_HOST_AUDIO_URL: str = 'audio_download/'
    OUTPUT_TEMPLATE: str = '%(uploader)s -- @%(extractor)s -- %(title)s -- %(upload_date>%Y-%m-%d)s.%(ext)s'
    OUTPUT_TEMPLATE_PLAYLIST: str = '%(playlist_title)s/%(title)s.%(ext)s'
    OUTPUT_TEMPLATE_CHANNEL: str = '%(channel)s/%(title)s.%(ext)s'
    DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT: int = 0
    CLEAR_COMPLETED_AFTER: int = 0
    YTDL_OPTIONS: dict[str, Any] = Field(default_factory=dict)
    YTDL_OPTIONS_FILE: str = ''
    YTDL_OPTIONS_PRESETS: dict[str, Any] = Field(default_factory=dict)
    ALLOW_YTDL_OPTIONS_OVERRIDES: bool = False
    CORS_ALLOWED_ORIGINS: str = ''
    HOST: str = '0.0.0.0'
    PORT: int = 8081
    BASE_DIR: str = ''
    MAX_CONCURRENT_DOWNLOADS: int = 3
    LOGLEVEL: str = 'INFO'
    YTDL_NIGHTLY_UPDATE_TIME: str = ''

    _runtime_overrides: dict[str, Any] = PrivateAttr(default_factory=dict)
    _ytdl_options_base: dict[str, Any] = PrivateAttr(default_factory=dict)

    _FRONTEND_KEYS: ClassVar[tuple[str, ...]] = (
        'PUBLIC_HOST_URL',
        'PUBLIC_HOST_AUDIO_URL',
        'DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT',
        'ALLOW_YTDL_OPTIONS_OVERRIDES',
    )

    def __init__(self, **kwargs: Any) -> None:
        try:
            super().__init__(**kwargs)
        except PydanticValidationError as exc:
            for err in exc.errors(include_url=False):
                field = '.'.join(str(p) for p in err.get('loc', ())) or 'unknown'
                log.error('Config error [%s]: %s', field, err['msg'])
            sys.exit(1)
        except SettingsError as exc:
            log.error('Config error: %s', exc)
            sys.exit(1)

    @field_validator('PORT')
    @classmethod
    def _valid_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError('must be between 1 and 65535')
        return v

    @field_validator('MAX_CONCURRENT_DOWNLOADS')
    @classmethod
    def _positive_downloads(cls, v: int) -> int:
        if v < 1:
            raise ValueError('must be >= 1')
        return v

    @field_validator('CLEAR_COMPLETED_AFTER')
    @classmethod
    def _non_negative_clear(cls, v: int) -> int:
        if v < 0:
            raise ValueError('must be >= 0')
        return v

    @field_validator('YTDL_NIGHTLY_UPDATE_TIME')
    @classmethod
    def _valid_nightly_time(cls, v: str) -> str:
        if v and not _NIGHTLY_TIME_RE.match(v):
            raise ValueError('must be HH:MM (24-hour format)')
        return v

    @field_validator('YTDL_OPTIONS_FILE')
    @classmethod
    def _resolve_options_file(cls, v: str) -> str:
        if v and v.startswith('.'):
            return str(Path(v).resolve())
        return v

    @field_validator('YTDL_OPTIONS_PRESETS')
    @classmethod
    def _validate_presets_shape(cls, v: dict) -> dict:
        for name, opts in v.items():
            if not isinstance(name, str) or not isinstance(opts, dict):
                raise ValueError('each entry must be a string name mapped to an options dict')
        return v

    @model_validator(mode='after')
    def _post_init(self) -> 'Config':
        if not self.AUDIO_DOWNLOAD_DIR:
            self.AUDIO_DOWNLOAD_DIR = self.DOWNLOAD_DIR
        if not self.TEMP_DIR:
            self.TEMP_DIR = self.DOWNLOAD_DIR
        # A blank PUBLIC_HOST_AUDIO_URL with a non-blank PUBLIC_HOST_URL would produce
        # root-relative audio links that 404. Fall back to the audio_download/ route.
        if not self.PUBLIC_HOST_AUDIO_URL and self.PUBLIC_HOST_URL:
            self.PUBLIC_HOST_AUDIO_URL = 'audio_download/'
        for attr in ('PUBLIC_HOST_URL', 'PUBLIC_HOST_AUDIO_URL'):
            val = getattr(self, attr)
            if val and not val.endswith('/'):
                setattr(self, attr, val + '/')
        if self.YTDL_OPTIONS_FILE:
            log.info('Loading yt-dlp custom options from "%s"', self.YTDL_OPTIONS_FILE)
            path = Path(self.YTDL_OPTIONS_FILE)
            if not path.exists():
                raise ValueError(f'YTDL_OPTIONS_FILE: file "{self.YTDL_OPTIONS_FILE}" not found')
            try:
                with path.open() as f:
                    opts = json.load(f)
                if not isinstance(opts, dict):
                    raise ValueError('YTDL_OPTIONS_FILE: contents must be a JSON object')
            except json.JSONDecodeError as exc:
                raise ValueError('YTDL_OPTIONS_FILE: not valid JSON') from exc
            self.YTDL_OPTIONS.update(opts)
        self._ytdl_options_base = dict(self.YTDL_OPTIONS)
        return self

    def set_runtime_override(self, key: str, value: Any) -> None:
        self._runtime_overrides[key] = value
        self.YTDL_OPTIONS[key] = value

    def remove_runtime_override(self, key: str) -> None:
        self._runtime_overrides.pop(key, None)
        self.YTDL_OPTIONS.pop(key, None)

    def _apply_runtime_overrides(self) -> None:
        self.YTDL_OPTIONS.update(self._runtime_overrides)

    def load_ytdl_options(self) -> tuple[bool, str]:
        """Reset YTDL_OPTIONS to its base state (env + file) and re-apply remaining runtime overrides."""
        self.YTDL_OPTIONS = dict(self._ytdl_options_base)
        self._apply_runtime_overrides()
        return (True, '')

    def frontend_safe(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self._FRONTEND_KEYS}


config = Config()
logging.getLogger().setLevel(parse_log_level(str(config.LOGLEVEL)) or logging.INFO)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AddRequest(BaseModel):
    url: str
    download_type: str
    quality: str
    format: str
    codec: str = 'auto'
    folder: str | None = None
    custom_name_prefix: str = ''
    playlist_item_limit: int | None = None
    auto_start: bool = True
    subtitle_langs: list[str] = Field(default_factory=list)
    ytdl_options_presets: list[str] = Field(default_factory=list)
    ytdl_options_overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator('url')
    @classmethod
    def _nonempty_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('must not be empty')
        return v.strip()

    @field_validator('download_type')
    @classmethod
    def _valid_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in VALID_DOWNLOAD_TYPES:
            raise ValueError(f'must be one of {sorted(VALID_DOWNLOAD_TYPES)}')
        return v

    @field_validator('codec')
    @classmethod
    def _valid_codec(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in VALID_VIDEO_CODECS:
            raise ValueError(f'must be one of {sorted(VALID_VIDEO_CODECS)}')
        return v

    @field_validator('custom_name_prefix')
    @classmethod
    def _no_traversal(cls, v: str) -> str:
        if v and ('..' in v or v.startswith('/') or v.startswith('\\')):
            raise ValueError('must not contain ".." or start with a path separator')
        return v

    @field_validator('subtitle_langs')
    @classmethod
    def _valid_langs(cls, v: list[str]) -> list[str]:
        for code in v:
            if not _SUBTITLE_LANG_RE.match(code):
                raise ValueError(f'invalid subtitle language code: {code!r}')
        return v

    @field_validator('ytdl_options_overrides', mode='before')
    @classmethod
    def _parse_overrides_json(cls, v: Any) -> dict:
        if v is None or v == '':
            return {}
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError('must be valid JSON') from exc
            if not isinstance(parsed, dict):
                raise ValueError('must be a JSON object')
            return parsed
        return v

    @model_validator(mode='after')
    def _validate_type_specific(self) -> 'AddRequest':
        dt = self.download_type
        fmt = (self.format or '').strip().lower()
        qual = (self.quality or '').strip().lower()

        if dt == 'video':
            if fmt not in VALID_VIDEO_FORMATS:
                raise ValueError(f'format must be one of {sorted(VALID_VIDEO_FORMATS)} for video')
            if qual not in VALID_VIDEO_QUALITIES:
                raise ValueError(f'quality must be one of {sorted(VALID_VIDEO_QUALITIES)} for video')
            self.format = fmt
            self.quality = qual
        elif dt == 'audio':
            if fmt not in VALID_AUDIO_FORMATS:
                raise ValueError(f'format must be one of {sorted(VALID_AUDIO_FORMATS)} for audio')
            allowed = {'best'}
            if fmt == 'mp3':
                allowed |= {'320', '192', '128'}
            elif fmt == 'm4a':
                allowed |= {'192', '128'}
            if qual not in allowed:
                raise ValueError(f'quality must be one of {sorted(allowed)} for {fmt}')
            self.format = fmt
            self.quality = qual
            self.codec = 'auto'
        elif dt in ('captions', 'thumbnail'):
            self.format = fmt
            self.quality = 'best'
            self.codec = 'auto'

        return self


class DeleteRequest(BaseModel):
    ids: list[str]
    where: Literal['queue', 'done']


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    status: Literal['ok', 'error'] = 'ok'
    msg: str | None = None


class CookieStatusResponse(BaseModel):
    status: Literal['ok'] = 'ok'
    has_cookies: bool


class PresetsResponse(BaseModel):
    presets: list[str]


class ConfigurationResponse(BaseModel):
    ALLOW_YTDL_OPTIONS_OVERRIDES: bool
    PUBLIC_HOST_URL: str
    PUBLIC_HOST_AUDIO_URL: str
    DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT: int


# ---------------------------------------------------------------------------
# WSGI / middleware infrastructure
# ---------------------------------------------------------------------------

_STATE_DIR_REAL = Path(config.STATE_DIR).resolve()


def _is_within_state_dir(target: str | Path) -> bool:
    return Path(target).is_relative_to(_STATE_DIR_REAL)


@web.middleware
async def state_dir_guard(request: web.Request, handler: Any) -> web.StreamResponse:
    for prefix, base in (
        ('/download/', config.DOWNLOAD_DIR),
        ('/audio_download/', config.AUDIO_DOWNLOAD_DIR),
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


def _first_validation_error(exc: PydanticValidationError) -> str:
    errors = exc.errors(include_url=False)
    return errors[0]['msg'] if errors else 'invalid request'


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

COOKIES_PATH = Path(config.STATE_DIR) / 'cookies.txt'


@routes.post('/add')
async def add(request: web.Request) -> web.Response:
    post = await _read_json_request(request)
    try:
        req = AddRequest.model_validate(post)
    except PydanticValidationError as exc:
        raise web.HTTPBadRequest(reason=_first_validation_error(exc)) from exc

    for preset_name in req.ytdl_options_presets:
        if preset_name not in config.YTDL_OPTIONS_PRESETS:
            raise web.HTTPBadRequest(reason='ytdl_options_presets must only contain configured preset names')
    if req.ytdl_options_overrides and not config.ALLOW_YTDL_OPTIONS_OVERRIDES:
        raise web.HTTPBadRequest(reason='ytdl_options_overrides are disabled')

    limit = req.playlist_item_limit if req.playlist_item_limit is not None else config.DEFAULT_OPTION_PLAYLIST_ITEM_LIMIT

    log.info(
        "Add download request: type=%s quality=%s format=%s has_folder=%s auto_start=%s",
        req.download_type, req.quality, req.format, bool(req.folder), req.auto_start,
    )
    status = await dqueue.add(
        req.url, req.download_type, req.codec, req.format, req.quality,
        req.folder, req.custom_name_prefix, limit, req.auto_start,
        subtitle_langs=req.subtitle_langs,
        ytdl_options_presets=req.ytdl_options_presets,
        ytdl_options_overrides=req.ytdl_options_overrides,
    )
    return web.json_response(status)


@routes.get('/presets')
async def presets(request: web.Request) -> web.Response:
    return web.json_response(PresetsResponse(presets=sorted(config.YTDL_OPTIONS_PRESETS.keys())).model_dump())


@routes.post('/delete')
async def delete(request: web.Request) -> web.Response:
    post = await _read_json_request(request)
    try:
        req = DeleteRequest.model_validate(post)
    except PydanticValidationError as exc:
        raise web.HTTPBadRequest(reason=_first_validation_error(exc)) from exc
    status = await (dqueue.cancel(req.ids) if req.where == 'queue' else dqueue.clear(req.ids))
    log.info(f"Download delete request processed for ids: {req.ids}, where: {req.where}")
    return web.json_response(status)


@routes.post('/cookies')
async def upload_cookies(request: web.Request) -> web.Response:
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != 'cookies':
        return web.json_response(StatusResponse(status='error', msg='No cookies file provided').model_dump(), status=400)

    max_size = 1_000_000
    size = 0
    content = bytearray()
    while True:
        chunk = await field.read_chunk()
        if not chunk:
            break
        size += len(chunk)
        if size > max_size:
            return web.json_response(StatusResponse(status='error', msg='Cookie file too large (max 1MB)').model_dump(), status=400)
        content.extend(chunk)

    tmp_cookie_path = COOKIES_PATH.with_name(COOKIES_PATH.name + '.tmp')
    tmp_cookie_path.write_bytes(bytes(content))
    # Cookies are sensitive auth material; restrict to owner read/write only.
    try:
        tmp_cookie_path.chmod(0o600)
    except OSError as exc:
        log.warning(f'Could not restrict permissions on cookies file: {exc}')
    tmp_cookie_path.replace(COOKIES_PATH)
    config.set_runtime_override('cookiefile', str(COOKIES_PATH))
    log.info(f'Cookies file uploaded ({size} bytes)')
    return web.json_response(StatusResponse(msg=f'Cookies uploaded ({size} bytes)').model_dump())


@routes.delete('/cookies')
async def delete_cookies(request: web.Request) -> web.Response:
    has_uploaded_cookies = COOKIES_PATH.exists()
    configured_cookiefile = config.YTDL_OPTIONS.get('cookiefile')
    has_manual_cookiefile = isinstance(configured_cookiefile, str) and configured_cookiefile and configured_cookiefile != str(COOKIES_PATH)

    if not has_uploaded_cookies:
        if has_manual_cookiefile:
            return web.json_response(
                StatusResponse(status='error', msg='Cookies are configured manually via YTDL_OPTIONS (cookiefile). Remove or change that setting manually; UI delete only removes uploaded cookies.').model_dump(),
                status=400,
            )
        return web.json_response(StatusResponse(status='error', msg='No uploaded cookies to delete').model_dump(), status=400)

    COOKIES_PATH.unlink()
    config.remove_runtime_override('cookiefile')
    success, msg = config.load_ytdl_options()
    if not success:
        log.error(f'Cookies file deleted, but failed to reload YTDL_OPTIONS: {msg}')
        return web.json_response(StatusResponse(status='error', msg=f'Cookies file deleted, but failed to reload YTDL_OPTIONS: {msg}').model_dump(), status=500)

    log.info('Cookies file deleted')
    return web.json_response(StatusResponse().model_dump())


@routes.get('/cookies')
async def cookie_status(request: web.Request) -> web.Response:
    configured_cookiefile = config.YTDL_OPTIONS.get('cookiefile')
    has_configured_cookies = isinstance(configured_cookiefile, str) and Path(configured_cookiefile).exists()
    has_uploaded_cookies = COOKIES_PATH.exists()
    return web.json_response(CookieStatusResponse(has_cookies=has_uploaded_cookies or has_configured_cookies).model_dump())


@routes.get('/queue')
async def queue_state(request: web.Request) -> web.Response:
    queue, done = dqueue.get()
    return web.Response(
        text=json.dumps([
            [[k, info.to_public_dict()] for k, info in queue],
            [[k, info.to_public_dict()] for k, info in done],
        ]),
        content_type='application/json',
    )


@routes.get('/logs')
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
    return web.Response(text=json.dumps(lines), content_type='application/json')


@routes.get('/configuration')
async def configuration(request: web.Request) -> web.Response:
    return web.json_response(ConfigurationResponse(**config.frontend_safe()).model_dump())


@routes.get('/')
async def index(request: web.Request) -> web.Response:
    return web.FileResponse(Path(config.BASE_DIR) / 'ui/index.html')


routes.static('/download/', config.DOWNLOAD_DIR)
routes.static('/audio_download/', config.AUDIO_DOWNLOAD_DIR)
routes.static('/', Path(config.BASE_DIR) / 'ui')
try:
    app.add_routes(routes)
except ValueError as e:
    if 'ui/index.html' in str(e) or 'ui' in str(e):
        raise RuntimeError('Could not find the frontend UI static assets. Expected ui/index.html') from e
    raise e


# https://github.com/aio-libs/aiohttp/pull/4615 waiting for release
# @routes.options('add')
async def add_cors(request):
    return web.json_response({'status': 'ok'})

app.router.add_route('OPTIONS', '/add', add_cors)
app.router.add_route('OPTIONS', '/cookies', add_cors)
app.router.add_route('OPTIONS', '/logs', add_cors)


async def on_prepare(request: web.Request, response: web.StreamResponse) -> None:
    origin = request.headers.get('Origin')
    if origin and _cors_origins and ('*' in _cors_origins or origin in _cors_origins):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'

app.on_response_prepare.append(on_prepare)

if __name__ == '__main__':
    logging.getLogger().setLevel(parse_log_level(config.LOGLEVEL) or logging.INFO)
    log.info(f"Listening on {config.HOST}:{config.PORT}")

    if COOKIES_PATH.exists():
        config.set_runtime_override('cookiefile', str(COOKIES_PATH))
        log.info(f'Cookie file detected at {COOKIES_PATH}')

    web.run_app(app, host=config.HOST, port=int(config.PORT))
    if _RESTART_FOR_UPDATE:
        sys.exit(42)
